#!/usr/bin/env bash
set -euo pipefail

APPLY=0
SETTINGS="${CLAUDE_SETTINGS_PATH:-$HOME/.claude/settings.json}"

for arg in "$@"; do
    case "$arg" in
        --apply)
            APPLY=1
            ;;
        --settings)
            echo "ERROR: --settings requires a path" >&2
            exit 2
            ;;
        --settings=*)
            SETTINGS="${arg#--settings=}"
            ;;
        -h|--help)
            cat <<'USAGE'
Usage: scripts/install-hooks.sh [--apply] [--settings=/path/to/settings.json]

Print the diff that adds the four claude-code-fleet-notify hooks to Claude Code
settings. Without --apply, no files are written. With --apply, a timestamped
backup is created before updating the settings file.
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

python3 - "$SETTINGS" "$REPO_DIR" "$APPLY" <<'PY'
from __future__ import annotations

import difflib
import json
import shutil
import sys
import time
from pathlib import Path

settings_path = Path(sys.argv[1]).expanduser()
repo_dir = Path(sys.argv[2]).resolve()
apply = sys.argv[3] == "1"

hook_specs = {
    "PreToolUse": ("pre_tool_activity.py", 3000),
    "PostToolUse": ("check_notifications.py", 5000),
    "Stop": ("stop_idle.py", 5000),
    "UserPromptSubmit": ("prompt_activity.py", 5000),
}

if settings_path.exists():
    original_text = settings_path.read_text()
    try:
        settings = json.loads(original_text)
    except json.JSONDecodeError as exc:
        print(f"ERROR: {settings_path} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
else:
    original_text = "{\n}\n"
    settings = {}

settings.setdefault("hooks", {})
for event, (script_name, timeout) in hook_specs.items():
    command = f"python3 {repo_dir / 'hooks' / script_name}"
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
    existing = [
        hook.get("command")
        for group in event_entries
        for hook in group.get("hooks", [])
        if isinstance(group, dict)
    ]
    if command not in existing:
        event_entries.append(entry)

new_text = json.dumps(settings, indent=2, sort_keys=False) + "\n"
diff = "".join(
    difflib.unified_diff(
        original_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=str(settings_path),
        tofile=str(settings_path),
    )
)

if diff:
    print(diff, end="")
else:
    print(f"No hook changes needed for {settings_path}.")

if not apply:
    print("Dry run only. Run with --apply to write settings.")
    sys.exit(0)

settings_path.parent.mkdir(parents=True, exist_ok=True)
timestamp = time.strftime("%Y%m%d-%H%M%S")
backup_path = settings_path.with_name(settings_path.name + f".bak.{timestamp}")
if settings_path.exists():
    shutil.copy2(settings_path, backup_path)
else:
    backup_path.write_text(original_text)
settings_path.write_text(new_text)
print(f"Applied hook settings. Backup: {backup_path}")
PY
