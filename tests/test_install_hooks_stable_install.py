"""Hook commands must reference a stable runtime root, never a checkout.

2026-06-11 outage class: settings.json hook commands pointed into a movable
checkout; when that path dangled, every hook command became
`python3 <missing file>` -> exit 2 = BLOCKING -> every tool of every session
on the machine was disabled at once.

Root-cause shape (supersedes the guard-wrap approach of PR#15, BLOCKed on
audit): the installer copies hooks/*.py to a stable install root and writes
plain quoted commands referencing ONLY the copies. There is nothing to
guard — moving/renaming/deleting the checkout cannot affect hook execution,
and the hook's own exit code (including an intentional Stop block) passes
through untouched because the command is a bare argv, not a shell compound.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REAL_REPO = Path(__file__).resolve().parent.parent
EVENTS = ("PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit")
# Event -> script basename, mirroring CLI_SPECS["claude"] in the installer.
SCRIPTS = {
    "PreToolUse": "pre_tool_activity.py",
    "PostToolUse": "check_notifications.py",
    "Stop": "stop_idle.py",
    "UserPromptSubmit": "prompt_activity.py",
}


def _make_checkout(td: Path) -> Path:
    """Build a scratch copy of the repo so tests can move/mutate it freely."""
    checkout = td / "checkout" / "claude-code-fleet-notify"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "hooks").mkdir()
    shutil.copy2(REAL_REPO / "scripts" / "install-hooks.sh",
                 checkout / "scripts" / "install-hooks.sh")
    for src in (REAL_REPO / "hooks").glob("*.py"):
        shutil.copy2(src, checkout / "hooks" / src.name)
    # Probe script: lets tests execute a runtime copy without needing the
    # real hooks' runtime dependencies (redis, orchestrator package).
    (checkout / "hooks" / "probe_stable_install.py").write_text(
        "print('PROBE-OK')\n"
    )
    (checkout / ".env").write_text("CF_TEST_SENTINEL=from-checkout\n")
    return checkout


def _run_installer(checkout: Path, sandbox: Path, *args: str,
                   apply: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_SETTINGS_PATH"] = str(sandbox / "settings.json")
    env["CODEX_HOOKS_PATH"] = str(sandbox / "codex-hooks.json")
    env["GEMINI_SETTINGS_PATH"] = str(sandbox / "gemini-settings.json")
    env["CF_INSTALL_DIR"] = str(sandbox / "runtime")
    cmd = ["bash", str(checkout / "scripts" / "install-hooks.sh"), *args]
    if apply:
        cmd.append("--apply")
    return subprocess.run(cmd, check=True, env=env, capture_output=True, text=True)


def _settings(sandbox: Path) -> dict:
    return json.loads((sandbox / "settings.json").read_text())


def _commands(settings: dict, event: str) -> list[str]:
    return [
        hook.get("command", "")
        for group in settings.get("hooks", {}).get(event, [])
        if isinstance(group, dict)
        for hook in group.get("hooks", [])
    ]


class StableInstallShape(unittest.TestCase):
    def test_commands_reference_runtime_root_only_plain_argv(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            checkout = _make_checkout(td)
            _run_installer(checkout, td)
            settings = _settings(td)
            settings_text = (td / "settings.json").read_text()
            self.assertNotIn(str(checkout), settings_text,
                             "settings must not reference the checkout")
            runtime_hooks = td / "runtime" / "hooks"
            for event in EVENTS:
                cmds = _commands(settings, event)
                self.assertEqual(len(cmds), 1, f"{event}: {cmds}")
                argv = shlex.split(cmds[0])
                self.assertEqual(argv[0], "python3")
                self.assertEqual(len(argv), 2,
                                 f"{event} command is not a bare argv: {cmds[0]}")
                self.assertEqual(Path(argv[1]), runtime_hooks / SCRIPTS[event])
                self.assertTrue(Path(argv[1]).is_file(),
                                f"runtime copy missing: {argv[1]}")

    def test_env_seeded_once_then_durable(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            checkout = _make_checkout(td)
            _run_installer(checkout, td)
            runtime_env = td / "runtime" / ".env"
            self.assertEqual(runtime_env.read_text(),
                             "CF_TEST_SENTINEL=from-checkout\n")
            # Operator edits the runtime .env; a later install must not touch it.
            runtime_env.write_text("CF_TEST_SENTINEL=operator-edited\n")
            (checkout / ".env").write_text("CF_TEST_SENTINEL=newer-checkout\n")
            _run_installer(checkout, td)
            self.assertEqual(runtime_env.read_text(),
                             "CF_TEST_SENTINEL=operator-edited\n")

    def test_migration_dedupes_all_historical_shapes_preserves_foreign(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            checkout = _make_checkout(td)
            old = "/old/moved/checkout/hooks"
            pre = {"hooks": {event: [
                # old bare form
                {"hooks": [{"type": "command",
                            "command": f"python3 {old}/{script}",
                            "timeout": 5000}]},
                # guard-wrapped form (PR#15 shape, in case any machine got it)
                {"hooks": [
                    {"type": "command",
                     "command": (f'if [ -f "{old}/{script}" ]; then python3 '
                                 f'"{old}/{script}"; else exit 0; fi'),
                     "timeout": 5000},
                    # foreign hook sharing a group with ours must survive
                    {"type": "command",
                     "command": "python3 /somewhere/other_tool.py",
                     "timeout": 1000},
                ]},
                # duplicate bare form
                {"hooks": [{"type": "command",
                            "command": f"python3 {old}/{script}",
                            "timeout": 5000}]},
            ] for event, script in SCRIPTS.items()}}
            (td / "settings.json").write_text(json.dumps(pre))
            _run_installer(checkout, td)
            settings = _settings(td)
            for event, script in SCRIPTS.items():
                cmds = _commands(settings, event)
                ours = [c for c in cmds if script in c]
                self.assertEqual(len(ours), 1,
                                 f"{event} not deduped to one entry: {cmds}")
                self.assertIn(str(td / "runtime"), ours[0])
                self.assertIn("python3 /somewhere/other_tool.py", cmds,
                              f"{event} dropped a foreign hook: {cmds}")

    def test_moved_checkout_cannot_affect_hook_execution(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            checkout = _make_checkout(td)
            _run_installer(checkout, td)
            settings = _settings(td)
            checkout.rename(td / "checkout" / "moved-away")
            for event in EVENTS:
                path = shlex.split(_commands(settings, event)[0])[1]
                self.assertTrue(Path(path).is_file(),
                                f"{event} runtime copy vanished with checkout")
            probe = subprocess.run(
                ["sh", "-c", f"python3 {shlex.quote(str(td / 'runtime' / 'hooks' / 'probe_stable_install.py'))}"],
                capture_output=True, text=True)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(probe.stdout.strip(), "PROBE-OK")

    def test_hook_exit_code_passes_through_untouched(self):
        # The command is a bare argv: an intentional blocking exit (Stop hook
        # exit 2, the keep-going mechanism) must reach the CLI unmodified.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            checkout = _make_checkout(td)
            (checkout / "hooks" / "probe_stable_install.py").write_text(
                "import sys; sys.exit(2)\n"
            )
            _run_installer(checkout, td)
            cmd = f"python3 {shlex.quote(str(td / 'runtime' / 'hooks' / 'probe_stable_install.py'))}"
            rc = subprocess.run(["sh", "-c", cmd]).returncode
            self.assertEqual(rc, 2)

    def test_second_run_is_a_noop(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            checkout = _make_checkout(td)
            _run_installer(checkout, td)
            first = (td / "settings.json").read_text()
            result = _run_installer(checkout, td)
            self.assertEqual((td / "settings.json").read_text(), first)
            self.assertIn("No hook changes needed", result.stdout)
            self.assertIn("are current", result.stdout)


if __name__ == "__main__":
    unittest.main()
