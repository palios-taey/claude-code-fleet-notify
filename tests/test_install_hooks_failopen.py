"""The hook command lines must fail-open ONLY when the hook file is missing.

2026-06-11: the canonical checkout path dangled (symlink target moved) -> every
hook command was `python3 <missing file>` -> exit 2 = BLOCKING -> every tool of
every session on the machine was disabled at once. The generated command now
guards on file existence: missing file -> exit 0 (silent no-op); file present ->
the hook's own exit passes through UNTOUCHED, so the Stop hook's intentional
exit-2 block (the keep-going mechanism) is never swallowed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "scripts" / "install-hooks.sh"
GUARD_RE = re.compile(r'^if \[ -f "(?P<path>[^"]+)" \]; then python3 "(?P=path)"; else exit 0; fi$')
EVENTS = ("PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit")


def _apply(settings_path: Path) -> dict:
    env = os.environ.copy()
    env["CLAUDE_SETTINGS_PATH"] = str(settings_path)
    # sandbox the other CLIs' paths too so the test never touches real configs
    env["CODEX_HOOKS_PATH"] = str(settings_path.parent / "codex-hooks.json")
    env["GEMINI_SETTINGS_PATH"] = str(settings_path.parent / "gemini-settings.json")
    subprocess.run(["bash", str(INSTALLER), "--apply"], check=True, env=env,
                   capture_output=True, text=True)
    return json.loads(settings_path.read_text())


def _commands(settings: dict, event: str) -> list[str]:
    return [
        hook.get("command", "")
        for group in settings.get("hooks", {}).get(event, [])
        if isinstance(group, dict)
        for hook in group.get("hooks", [])
    ]


class FailOpenCommandShape(unittest.TestCase):
    def test_generated_commands_are_file_guarded(self):
        with tempfile.TemporaryDirectory() as td:
            settings = _apply(Path(td) / "settings.json")
            for event in EVENTS:
                cmds = _commands(settings, event)
                self.assertTrue(cmds, f"no {event} hook generated")
                for cmd in cmds:
                    self.assertRegex(cmd, GUARD_RE, f"{event} command not file-guarded: {cmd}")

    def test_guard_behavior_missing_present_and_intentional_block(self):
        with tempfile.TemporaryDirectory() as td:
            settings = _apply(Path(td) / "settings.json")
            real_cmd = _commands(settings, "PreToolUse")[0]
            real_path = GUARD_RE.match(real_cmd).group("path")

            def run(cmd: str) -> int:
                return subprocess.run(["sh", "-c", cmd], input="{}",
                                      capture_output=True, text=True).returncode

            # missing file -> exit 0 (the outage class, now fail-open)
            missing = real_cmd.replace(real_path, str(Path(td) / "nope.py"))
            self.assertEqual(run(missing), 0)

            # present file exiting 2 -> exit 2 passes through (Stop's intentional
            # block must never be swallowed by the guard)
            blocker = Path(td) / "block.py"
            blocker.write_text("import sys; sys.exit(2)\n")
            self.assertEqual(run(real_cmd.replace(real_path, str(blocker))), 2)

            # present file exiting 0 -> 0
            ok = Path(td) / "ok.py"
            ok.write_text("import sys; sys.exit(0)\n")
            self.assertEqual(run(real_cmd.replace(real_path, str(ok))), 0)

    def test_old_bare_commands_migrate_in_place_no_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            settings_path = Path(td) / "settings.json"
            hooks_dir = REPO / "hooks"
            bare = f"python3 {hooks_dir / 'pre_tool_activity.py'}"
            settings_path.write_text(json.dumps({
                "hooks": {"PreToolUse": [
                    {"hooks": [{"type": "command", "command": bare, "timeout": 3000}]}
                ]}
            }))
            settings = _apply(settings_path)
            cmds = _commands(settings, "PreToolUse")
            ours = [c for c in cmds if "pre_tool_activity.py" in c]
            self.assertEqual(len(ours), 1, f"format migration duplicated the hook: {ours}")
            self.assertRegex(ours[0], GUARD_RE)

    def test_apply_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            settings_path = Path(td) / "settings.json"
            first = _apply(settings_path)
            second = _apply(settings_path)
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
