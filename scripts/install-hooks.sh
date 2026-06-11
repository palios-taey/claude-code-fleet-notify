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
edit <install-dir>/.env to change live hook configuration.

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

import difflib
import json
import shlex
import shutil
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
    for src in sorted(hooks_src.glob("*.py")):
        dest = hooks_dir / src.name
        if dest.exists() and dest.read_bytes() == src.read_bytes():
            continue
        actions.append(f"copy {src.name} -> {dest}")
        if apply:
            hooks_dir.mkdir(parents=True, exist_ok=True)
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
if missing:
    raise SystemExit(
        f"ERROR: incomplete checkout — required hook scripts missing from "
        f"{hooks_src}: {', '.join(missing)}. Nothing was copied or written."
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
