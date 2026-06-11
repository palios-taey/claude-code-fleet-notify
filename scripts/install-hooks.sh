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
        -h|--help)
            cat <<'USAGE'
Usage: scripts/install-hooks.sh [--apply] [--codex] [--gemini] [--all]
                                [--settings=...] [--codex-settings=...]
                                [--gemini-settings=...]

Install fleet-notify hooks for the chosen REPL CLIs.

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
        "$INSTALL_CLAUDE" "$INSTALL_CODEX" "$INSTALL_GEMINI" <<'PY'
from __future__ import annotations

import difflib
import json
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

hooks_dir = repo_dir / "hooks"

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
        script_path = hooks_dir / script_name
        bare_command = f"python3 {script_path}"
        # Fail-open ONLY when the hook FILE is missing: a moved/deleted checkout
        # must never block every tool machine-wide (python3 on a missing file
        # exits 2 = BLOCKING; 2026-06-11 this disabled all tools of all sessions
        # at once). When the file EXISTS, the hook's own exit code passes through
        # untouched — including the Stop hook's intentional exit-2 block, which
        # is the keep-going mechanism and must never be swallowed.
        command = f'if [ -f "{script_path}" ]; then python3 "{script_path}"; else exit 0; fi'
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
        # Migration + idempotency: an entry for this script may exist in the OLD
        # bare format. Rewrite it in place (never append a second one — a format
        # change must not double-fire hooks fleet-wide).
        migrated = False
        for group in event_entries:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks", []):
                if hook.get("command") in (bare_command, command):
                    hook["command"] = command
                    hook["timeout"] = timeout
                    migrated = True
        if not migrated:
            event_entries.append(entry)

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
print("       and point it at hooks/grok_session_start.py, grok_stop.py,")
print("       and grok_user_prompt.py. See docs/grok-hooks.md.")

if not apply:
    print("")
    print("Dry run only. Run with --apply to write settings.")
PY
