#!/usr/bin/env bash
set -euo pipefail

# Install fleet-notify hooks for one or more REPL CLIs.
#
# Default: Claude Code only (~/.claude/settings.json).
# Flags select additional CLIs whose hook configs should also be wired.
#
# Grok uses its own dedicated global hook file at ~/.grok/hooks/cf-notify.json.
# This script does not install it; see docs/grok-hooks.md.

APPLY=0
INSTALL_CLAUDE=1
INSTALL_CODEX=0
INSTALL_GEMINI=0
CLAUDE_SETTINGS="${CLAUDE_SETTINGS_PATH:-$HOME/.claude/settings.json}"
CODEX_HOOKS="${CODEX_HOOKS_PATH:-$HOME/.codex/hooks.json}"
GEMINI_SETTINGS="${GEMINI_SETTINGS_PATH:-$HOME/.gemini/settings.json}"
# Stable runtime root. Hook commands in settings files reference ONLY this
# location — never the checkout this script runs from — so moving, renaming,
# or deleting a source checkout can never break a CLI's hook execution.
INSTALL_ROOT="${CF_INSTALL_DIR:-$HOME/.local/share/claude-code-fleet-notify/hooks-runtime}"

for arg in "$@"; do
    case "$arg" in
        --apply)
            APPLY=1
            ;;
        --codex)
            INSTALL_CODEX=1
            ;;
        --gemini)
            INSTALL_GEMINI=1
            ;;
        --all)
            INSTALL_CODEX=1
            INSTALL_GEMINI=1
            ;;
        --claude-only)
            INSTALL_CODEX=0
            INSTALL_GEMINI=0
            ;;
        --settings=*)
            CLAUDE_SETTINGS="${arg#--settings=}"
            ;;
        --codex-settings=*)
            CODEX_HOOKS="${arg#--codex-settings=}"
            ;;
        --gemini-settings=*)
            GEMINI_SETTINGS="${arg#--gemini-settings=}"
            ;;
        --install-dir=*)
            INSTALL_ROOT="${arg#--install-dir=}"
            ;;
        -h|--help)
            cat <<'USAGE'
Usage: scripts/install-hooks.sh [--apply] [--codex] [--gemini] [--all]
                                [--settings=...] [--codex-settings=...]
                                [--gemini-settings=...] [--install-dir=...]

Install fleet-notify hooks for the chosen REPL CLIs.

Hook scripts are copied to a stable runtime root (default
~/.local/share/claude-code-fleet-notify/hooks-runtime, override with
--install-dir= or CF_INSTALL_DIR). Settings files reference ONLY the
runtime copies, so moving or deleting a source checkout never affects
hook execution. A .env beside the runtime hooks is seeded from the
checkout's .env on first install and never overwritten afterwards —
edit $CF_INSTALL_DIR/.env (or the --install-dir path) to change live hook
configuration.

CLIs (each writes to its own config file format):
  Default (always)    Claude Code  → ~/.claude/settings.json (JSON)
  --codex             OpenAI codex → ~/.codex/hooks.json     (JSON)
  --gemini            Google gemini→ ~/.gemini/settings.json (JSON, different event names)
  --all               codex + gemini

Grok (xAI grok-cli) should use ~/.grok/hooks/cf-notify.json.
This installer does not wire Grok; see docs/grok-hooks.md.

Without --apply, no files are written (dry-run). With --apply, a
timestamped backup is created before updating each settings file.

Examples:
  install-hooks.sh                       # dry-run Claude Code only
  install-hooks.sh --apply               # install Claude Code only
  install-hooks.sh --all --apply         # install Claude Code + codex + gemini
  install-hooks.sh --codex --gemini      # dry-run codex + gemini (skip Claude Code)
                                         #  -- use with --claude-only=false implicitly
USAGE
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $arg" >&2
            exit 2
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

python3 - "$REPO_DIR" "$APPLY" "$CLAUDE_SETTINGS" "$CODEX_HOOKS" "$GEMINI_SETTINGS" \
        "$INSTALL_CLAUDE" "$INSTALL_CODEX" "$INSTALL_GEMINI" "$INSTALL_ROOT" <<'PY'
from __future__ import annotations

import ast
import difflib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

repo_dir = Path(sys.argv[1]).resolve()
apply = sys.argv[2] == "1"
claude_path = Path(sys.argv[3]).expanduser()
codex_path = Path(sys.argv[4]).expanduser()
gemini_path = Path(sys.argv[5]).expanduser()
do_claude = sys.argv[6] == "1"
do_codex = sys.argv[7] == "1"
do_gemini = sys.argv[8] == "1"
install_root = Path(sys.argv[9]).expanduser()

# Source of hook code: the checkout this script runs from.
# Runtime location referenced by settings files: the stable install root.
# The split is the point — settings must never depend on a movable checkout
# (2026-06-11: a dangling checkout path turned every hook command into
# `python3 <missing file>` = exit 2 = every tool on the machine blocked).
hooks_src = repo_dir / "hooks"
hooks_dir = install_root / "hooks"

# The hooks import beyond their own directory: the `notifications` package
# and root-level `identity.py` (hooks/_shared.py bootstraps sys.path to the
# runtime root's parent layout, mirroring the repo). The runtime root must
# carry the FULL import closure — 2026-06-11 rollout proved a hooks-only
# copy boots to ModuleNotFoundError on every hook (silent fleet-wide
# notification loss). The boot gate below is the drift-proof invariant;
# this list is the known layout.
RUNTIME_PACKAGES = ["notifications"]
RUNTIME_ROOT_FILES = ["identity.py"]


def runs_script(command: str, script_name: str) -> bool:
    """True if a settings hook command executes this hook script.

    Matches by argv-token basename so every historical form is caught
    (bare checkout paths, guard-wrapped compounds, any directory), while
    commands that merely mention the name inside a longer token
    (stop_idle.py.bak, stop_idle.py.log) are NOT ours and must survive.
    """
    if script_name not in command:
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unparseable command that mentions our script: do not guess.
        # Leave it untouched and say so — the operator decides.
        print(f"WARNING: unparseable hook command left untouched: {command}",
              file=sys.stderr)
        return False
    return any(Path(token).name == script_name for token in tokens)


def sync_runtime() -> list[str]:
    """Refresh runtime hook copies; seed .env only on first install.

    Hook .py files are always refreshed from the checkout (an install IS
    the update mechanism). The runtime .env is durable operator state:
    seeded from the checkout's .env when absent, never overwritten.
    Returns human-readable action lines (empty = nothing to do).
    """
    actions: list[str] = []

    def copy_tree(src_dir: Path, dest_dir: Path, label: str) -> None:
        for src in sorted(src_dir.rglob("*.py")):
            rel = src.relative_to(src_dir)
            dest = dest_dir / rel
            if dest.exists() and dest.read_bytes() == src.read_bytes():
                continue
            actions.append(f"copy {label}{rel.as_posix()} -> {dest}")
            if apply:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)

    copy_tree(hooks_src, hooks_dir, "")
    for package in RUNTIME_PACKAGES:
        copy_tree(repo_dir / package, install_root / package, f"{package}/")
    for name in RUNTIME_ROOT_FILES:
        src = repo_dir / name
        dest = install_root / name
        if not (dest.exists() and dest.read_bytes() == src.read_bytes()):
            actions.append(f"copy {name} -> {dest}")
            if apply:
                install_root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
    env_src = repo_dir / ".env"
    env_dest = install_root / ".env"
    if not env_dest.exists():
        if env_src.exists():
            actions.append(f"seed .env -> {env_dest} (first install; never overwritten after)")
            if apply:
                install_root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(env_src, env_dest)
        else:
            actions.append(
                f"NOTE: no .env at {env_dest} and no {env_src} to seed it from; "
                "hooks will rely on process environment only (see .env.example)"
            )
    return actions

# Per-CLI hook specs. Each entry: (config file, format, event-to-script map, timeout)
# All three CLIs read JSON; only event names + script names differ.
# Codex + Claude Code use the same event names; Gemini uses BeforeTool/
# AfterTool/BeforeAgent/AfterAgent.
CLI_SPECS = {
    "claude": {
        "path": claude_path,
        "hooks": {
            "SessionStart":     ("session_start.py",       5000),
            "PreToolUse":       ("pre_tool_activity.py",   3000),
            "PostToolUse":      ("check_notifications.py", 5000),
            "Stop":             ("stop_idle.py",           5000),
            "UserPromptSubmit": ("prompt_activity.py",     5000),
        },
        "enabled": do_claude,
    },
    "codex": {
        "path": codex_path,
        "hooks": {
            "SessionStart":     ("codex_session_start.py", 10000),
            "PreToolUse":       ("codex_pre_tool.py",      10000),
            "PostToolUse":      ("codex_post_tool.py",     10000),
            "Stop":             ("codex_stop.py",          10000),
            "UserPromptSubmit": ("codex_user_prompt.py",   10000),
        },
        "enabled": do_codex,
    },
    "gemini": {
        "path": gemini_path,
        "hooks": {
            "BeforeTool":   ("gemini_before_tool.py",   10000),
            "AfterTool":    ("gemini_after_tool.py",    10000),
            "BeforeAgent":  ("gemini_before_agent.py",  10000),
            "AfterAgent":   ("gemini_after_agent.py",   10000),
        },
        "enabled": do_gemini,
    },
}


def patch_one(cli_name: str, spec: dict) -> tuple[str, str, str]:
    """Returns (diff_text, original_text, new_text) for one CLI's config."""
    settings_path = spec["path"]
    hook_specs = spec["hooks"]

    if settings_path.exists():
        original_text = settings_path.read_text()
        try:
            settings = json.loads(original_text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"ERROR: {settings_path} is not valid JSON: {exc}")
    else:
        original_text = "{\n}\n"
        settings = {}

    settings.setdefault("hooks", {})
    for event, (script_name, timeout) in hook_specs.items():
        # Plain argv command — a quoted path and nothing else. No shell
        # compounds, no conditionals: the hook's own exit code (including an
        # intentional Stop block) passes through untouched.
        command = f"python3 {shlex.quote(str(hooks_dir / script_name))}"
        entry = {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": timeout,
                }
            ]
        }
        event_entries = settings["hooks"].setdefault(event, [])
        # Migration + dedupe: drop every existing hook that runs this script
        # under ANY historical path or wrapping (old checkout paths,
        # guard-wrapped forms, duplicates), preserve unrelated hooks and
        # group attributes, then append exactly one canonical entry.
        kept = []
        for group in event_entries:
            if not isinstance(group, dict):
                kept.append(group)
                continue
            group_hooks = [
                hook for hook in group.get("hooks", [])
                if not runs_script(str(hook.get("command", "")), script_name)
            ]
            if group_hooks:
                new_group = dict(group)
                new_group["hooks"] = group_hooks
                kept.append(new_group)
            elif "hooks" not in group:
                kept.append(group)
        kept.append(entry)
        settings["hooks"][event] = kept

    new_text = json.dumps(settings, indent=2, sort_keys=False) + "\n"
    diff_text = "".join(
        difflib.unified_diff(
            original_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"{cli_name}: {settings_path}",
            tofile=f"{cli_name}: {settings_path}",
        )
    )
    return diff_text, original_text, new_text


def apply_one(cli_name: str, spec: dict, original_text: str, new_text: str) -> None:
    settings_path = spec["path"]
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = settings_path.with_name(settings_path.name + f".bak.{timestamp}")
    if settings_path.exists():
        shutil.copy2(settings_path, backup_path)
    else:
        backup_path.write_text(original_text)
    settings_path.write_text(new_text)
    print(f"[{cli_name}] Applied. Backup: {backup_path}")


def source_package_parts(source: Path) -> list[str]:
    try:
        rel = source.resolve().relative_to(repo_dir)
    except ValueError:
        return []
    if len(rel.parts) < 2 or rel.suffix != ".py":
        return []
    if rel.parts[0] == "hooks":
        return ["hooks"] if rel.name == "__init__.py" else ["hooks", *rel.with_suffix("").parts[1:-1]]
    if rel.parts[0] in RUNTIME_PACKAGES:
        return [rel.parts[0], *rel.with_suffix("").parts[1:-1]]
    return []


def resolve_relative_import(source: Path, level: int, module: str | None,
                            alias: str | None) -> str | None:
    package = source_package_parts(source)
    if not package or level > len(package):
        return None
    base = package[:len(package) - level + 1]
    if module:
        base.extend(module.split("."))
    if alias:
        base.append(alias)
    return ".".join(part for part in base if part)


def is_our_module(module: str) -> bool:
    root = module.split(".", 1)[0]
    return root in ({"hooks", "notifications", "identity", "_shared"} | set(RUNTIME_PACKAGES))


def runtime_module_candidates(module: str) -> list[Path]:
    parts = module.split(".")
    if parts[0] == "identity":
        return [install_root / "identity.py"]
    if parts[0] == "_shared":
        return [hooks_dir / "_shared.py"]
    if parts[0] == "hooks":
        base = install_root.joinpath(*parts)
        return [base.with_suffix(".py"), base / "__init__.py"]
    if parts[0] in RUNTIME_PACKAGES:
        base = install_root.joinpath(*parts)
        return [base.with_suffix(".py"), base / "__init__.py"]
    return []


def runtime_module_exists(module: str) -> bool:
    return any(candidate.is_file() for candidate in runtime_module_candidates(module))


def runtime_module_is_package(module: str) -> bool:
    parts = module.split(".")
    if parts[0] not in ({"hooks"} | set(RUNTIME_PACKAGES)):
        return False
    return (install_root.joinpath(*parts) / "__init__.py").is_file()


def import_from_targets(module: str, aliases: list[ast.alias]) -> list[str]:
    if module in (set(RUNTIME_PACKAGES) | {"hooks"}) or runtime_module_is_package(module):
        return [f"{module}.{alias.name}" for alias in aliases
                if alias.name != "*"]
    return [module] if module else []


def import_targets(source: Path, node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if node.level:
        module = resolve_relative_import(source, node.level, node.module, None)
        if module:
            return import_from_targets(module, node.names)
        return [
            target for target in (
                resolve_relative_import(source, node.level, None, alias.name)
                for alias in node.names if alias.name != "*")
            if target]
    module = node.module or ""
    return import_from_targets(module, node.names)


def ast_import_closure_failures(sources: list[Path]) -> list[str]:
    failures: list[str] = []
    seen: set[tuple[Path, str]] = set()
    for source in sources:
        try:
            tree = ast.parse(source.read_text(), filename=str(source))
        except SyntaxError as exc:
            failures.append(f"{source.relative_to(repo_dir)}: invalid Python: {exc}")
            continue
        for node in ast.walk(tree):
            for target in import_targets(source, node):
                key = (source, target)
                if key in seen:
                    continue
                seen.add(key)
                if is_our_module(target) and not runtime_module_exists(target):
                    rel = source.relative_to(repo_dir)
                    failures.append(f"{rel}: imports {target}, but it is not in {install_root}")
    return failures


def runtime_closure_sources() -> list[Path]:
    sources = {hooks_src / name for name in required if name.endswith(".py")}
    for package in RUNTIME_PACKAGES:
        sources.update((repo_dir / package).rglob("*.py"))
    for name in RUNTIME_ROOT_FILES:
        sources.add(repo_dir / name)
    return sorted(sources)


# Completeness gate: every script any enabled CLI's settings will reference
# must exist in the source checkout BEFORE anything is copied or written.
# An incomplete checkout must fail loud here — never emit a command for a
# file that will not exist at the runtime root (the outage class this
# installer exists to make unreachable).
required = sorted({
    script_name
    for spec in CLI_SPECS.values() if spec["enabled"]
    for script_name, _timeout in spec["hooks"].values()
} | {"_shared.py"})  # imported by every hook script
missing = [name for name in required if not (hooks_src / name).is_file()]
missing += [
    f"{package}/__init__.py" for package in RUNTIME_PACKAGES
    if not (repo_dir / package / "__init__.py").is_file()
]
missing += [name for name in RUNTIME_ROOT_FILES
            if not (repo_dir / name).is_file()]
if missing:
    raise SystemExit(
        f"ERROR: incomplete checkout — required runtime sources missing from "
        f"{repo_dir}: {', '.join(missing)}. Nothing was copied or written."
    )

# Sync runtime copies FIRST: settings must never point at files that were
# not written. On dry-run this only reports what would be copied.
sync_actions = sync_runtime()
if sync_actions:
    suffix = "" if apply else " (dry-run, nothing written)"
    print(f"=== runtime sync: {install_root}{suffix} ===")
    for line in sync_actions:
        print(line)
else:
    print(f"[runtime] Hook copies at {install_root} are current.")

# Boot gate (apply mode): every runtime hook a settings file will reference
# must PROVE it imports cleanly from the runtime root before any settings
# file is written. A static copy-list drifts (2026-06-11: hooks-only copy
# shipped, every hook crashed on a missing package import while tests and
# two audits passed); execution does not drift. All hook scripts are
# __main__-guarded, so importing the module exercises the full import
# closure without running hook logic.
if apply:
    ast_failures = ast_import_closure_failures(runtime_closure_sources())
    if ast_failures:
        raise SystemExit(
            "ERROR: runtime AST import closure gate failed — these in-closure "
            "sources reference our modules that were not copied to the runtime "
            "root; NO settings were written:\n\n"
            + "\n".join(ast_failures)
        )
    print(f"[boot-gate] AST import closure verified for {len(runtime_closure_sources())} sources.")

    boot_failures = []
    probed = 0
    for script_name in required:
        if not script_name.endswith(".py") or script_name.startswith("_"):
            continue
        probed += 1
        runtime_script = hooks_dir / script_name
        probe = (
            "import importlib.util\n"
            f"spec = importlib.util.spec_from_file_location('cf_boot_probe', {str(runtime_script)!r})\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
        )
        # Faithful to real hook execution: a CLI runs hooks from an
        # arbitrary cwd with no PYTHONPATH help. Probe from a neutral cwd
        # so the checkout (often the installer's cwd) cannot satisfy an
        # import the runtime root is missing — that would false-pass the
        # gate and crash at real exec time.
        probe_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL, cwd="/", env=probe_env,
        )
        if proc.returncode != 0:
            boot_failures.append(f"{script_name}:\n{proc.stderr.strip()}")
    if boot_failures:
        raise SystemExit(
            "ERROR: runtime boot gate failed — these hooks do not import "
            "cleanly from the runtime root; NO settings were written:\n\n"
            + "\n\n".join(boot_failures)
        )
    print(f"[boot-gate] {probed} runtime hooks import cleanly.")

# Process each enabled CLI.
any_enabled = False
for cli_name, spec in CLI_SPECS.items():
    if not spec["enabled"]:
        continue
    any_enabled = True
    diff_text, original_text, new_text = patch_one(cli_name, spec)
    if diff_text:
        print(f"=== {cli_name}: diff for {spec['path']} ===")
        print(diff_text, end="")
        if not diff_text.endswith("\n"):
            print()
    else:
        print(f"[{cli_name}] No hook changes needed for {spec['path']}.")

    if apply:
        apply_one(cli_name, spec, original_text, new_text)

if not any_enabled:
    print("No CLI selected. Use at least one of: (default Claude Code) "
          "--codex --gemini --all", file=sys.stderr)
    sys.exit(2)

# Grok note — always print so users know where to wire it.
print("")
print("[grok] Separate wiring required: use ~/.grok/hooks/cf-notify.json")
print(f"       and point it at {hooks_dir}/grok_session_start.py,")
print(f"       grok_stop.py, and grok_user_prompt.py (the runtime copies,")
print(f"       not the checkout). See docs/grok-hooks.md.")

if not apply:
    print("")
    print("Dry run only. Run with --apply to write settings.")
PY
