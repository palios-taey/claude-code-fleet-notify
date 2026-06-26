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
EVENTS = ("SessionStart", "PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit")
# Event -> script basename(s), mirroring CLI_SPECS["claude"] in the installer.
SCRIPTS = {
    "SessionStart": ("session_start.py",),
    "PreToolUse": ("pre_tool_activity.py", "pre_tool_live_guard.py"),
    "PostToolUse": ("check_notifications.py",),
    "Stop": ("stop_idle.py",),
    "UserPromptSubmit": ("prompt_activity.py",),
}


def _make_checkout(td: Path) -> Path:
    """Build a scratch copy of the repo so tests can move/mutate it freely."""
    checkout = td / "checkout" / "claude-code-fleet-notify"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "hooks").mkdir()
    (checkout / "notifications").mkdir()
    shutil.copy2(REAL_REPO / "scripts" / "install-hooks.sh",
                 checkout / "scripts" / "install-hooks.sh")
    for src in (REAL_REPO / "hooks").glob("*.py"):
        shutil.copy2(src, checkout / "hooks" / src.name)
    for src in (REAL_REPO / "notifications").rglob("*.py"):
        dest = checkout / "notifications" / src.relative_to(REAL_REPO / "notifications")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    shutil.copy2(REAL_REPO / "identity.py", checkout / "identity.py")
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
                self.assertEqual(len(cmds), len(SCRIPTS[event]), f"{event}: {cmds}")
                for cmd, script in zip(cmds, SCRIPTS[event]):
                    argv = shlex.split(cmd)
                    self.assertEqual(argv[0], "python3")
                    self.assertEqual(len(argv), 2,
                                     f"{event} command is not a bare argv: {cmd}")
                    self.assertEqual(Path(argv[1]), runtime_hooks / script)
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
            pre = {"hooks": {}}
            for event, scripts in SCRIPTS.items():
                entries = []
                for script in scripts:
                    entries.extend([
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
                    ])
                pre["hooks"][event] = entries
            (td / "settings.json").write_text(json.dumps(pre))
            _run_installer(checkout, td)
            settings = _settings(td)
            for event, scripts in SCRIPTS.items():
                cmds = _commands(settings, event)
                for script in scripts:
                    ours = [c for c in cmds if script in c]
                    self.assertEqual(len(ours), 1,
                                     f"{event} {script} not deduped to one entry: {cmds}")
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
                for command in _commands(settings, event):
                    path = shlex.split(command)[1]
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

    def test_foreign_hooks_mentioning_script_names_survive(self):
        # Audit finding (grok R5 @88fa484): a substring dedupe would drop
        # foreign hooks whose command merely mentions one of our script
        # names inside a longer token. Match must be by argv-token basename.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            checkout = _make_checkout(td)
            foreign = [
                "python3 /backups/stop_idle.py.bak",
                "tail -n 5 /var/log/stop_idle.py.log",
                "python3 /x/pre_tool_activity.py.disabled",
            ]
            pre = {"hooks": {"Stop": [
                {"hooks": [{"type": "command", "command": cmd, "timeout": 1000}
                           for cmd in foreign]},
                {"hooks": [{"type": "command",
                            "command": "python3 /old/hooks/stop_idle.py",
                            "timeout": 5000}]},
            ]}}
            (td / "settings.json").write_text(json.dumps(pre))
            _run_installer(checkout, td)
            cmds = _commands(_settings(td), "Stop")
            for cmd in foreign:
                self.assertIn(cmd, cmds, f"foreign hook dropped: {cmd}")
            ours = [c for c in cmds if c.endswith("stop_idle.py'")
                    or shlex.split(c)[-1].endswith("/stop_idle.py")]
            self.assertEqual(len(ours), 1, cmds)

    def test_incomplete_checkout_refuses_before_writing_anything(self):
        # Audit finding (grok R5 @88fa484): an incomplete source checkout
        # must fail loud BEFORE any copy or settings write — never emit a
        # command for a runtime file that will not exist.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            checkout = _make_checkout(td)
            (checkout / "hooks" / "stop_idle.py").unlink()
            original = '{"keep": "me"}'
            (td / "settings.json").write_text(original)
            with self.assertRaises(subprocess.CalledProcessError) as ctx:
                _run_installer(checkout, td)
            self.assertIn("incomplete checkout", ctx.exception.stderr)
            self.assertIn("stop_idle.py", ctx.exception.stderr)
            self.assertEqual((td / "settings.json").read_text(), original,
                             "settings were modified despite refusal")
            self.assertFalse((td / "runtime").exists(),
                             "runtime files were written despite refusal")

    def test_runtime_hooks_import_cleanly_from_runtime_root(self):
        # 2026-06-11 rollout failure: a hooks-only runtime copy shipped and
        # every hook crashed importing the `notifications` package — tests
        # and two audits passed because nothing ever EXECUTED a real hook
        # from the runtime root. This does.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            checkout = _make_checkout(td)
            _run_installer(checkout, td)
            for script in ("stop_idle.py", "check_notifications.py"):
                target = td / "runtime" / "hooks" / script
                probe = (
                    "import importlib.util\n"
                    f"spec = importlib.util.spec_from_file_location('p', {str(target)!r})\n"
                    "m = importlib.util.module_from_spec(spec)\n"
                    "spec.loader.exec_module(m)\n"
                )
                proc = subprocess.run(["python3", "-c", probe],
                                      capture_output=True, text=True,
                                      stdin=subprocess.DEVNULL)
                self.assertEqual(proc.returncode, 0,
                                 f"{script} does not boot from runtime root:\n{proc.stderr}")

    def test_missing_package_module_fails_boot_gate_settings_untouched(self):
        # The drift case a static file-list cannot see: the package exists
        # (static gate passes) but a module the hooks import is gone. Only
        # the boot gate catches this — and it must refuse before settings.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            checkout = _make_checkout(td)
            (checkout / "notifications" / "inbox.py").unlink()
            original = '{"keep": "me"}'
            (td / "settings.json").write_text(original)
            with self.assertRaises(subprocess.CalledProcessError) as ctx:
                _run_installer(checkout, td)
            self.assertIn("AST import closure gate failed", ctx.exception.stderr)
            self.assertIn("notifications.inbox", ctx.exception.stderr)
            self.assertEqual((td / "settings.json").read_text(), original,
                             "settings were modified despite boot-gate refusal")

    def test_lazy_out_of_closure_import_fails_settings_untouched(self):
        # exec_module only sees top-level imports. A lazy in-function import
        # of our own runtime closure must be caught before settings point at
        # a hook that can crash on its first real call.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            checkout = _make_checkout(td)
            hook = checkout / "hooks" / "stop_idle.py"
            hook.write_text(
                hook.read_text()
                + "\n\ndef _lazy_missing_runtime_import():\n"
                  "    from notifications import missing_lazy\n"
                  "    return missing_lazy\n"
            )
            original = '{"keep": "me"}'
            (td / "settings.json").write_text(original)
            with self.assertRaises(subprocess.CalledProcessError) as ctx:
                _run_installer(checkout, td)
            self.assertIn("AST import closure gate failed", ctx.exception.stderr)
            self.assertIn("notifications.missing_lazy", ctx.exception.stderr)
            self.assertEqual((td / "settings.json").read_text(), original,
                             "settings were modified despite AST-gate refusal")

    def test_runtime_package_subpackages_are_copied(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            checkout = _make_checkout(td)
            subpkg = checkout / "notifications" / "subpkg"
            subpkg.mkdir()
            (subpkg / "__init__.py").write_text("")
            (subpkg / "tool.py").write_text("VALUE = 'copied'\n")
            hook = checkout / "hooks" / "stop_idle.py"
            hook.write_text(
                hook.read_text()
                + "\n\ndef _lazy_subpackage_import():\n"
                  "    from notifications.subpkg import tool\n"
                  "    return tool.VALUE\n"
            )
            _run_installer(checkout, td)
            self.assertEqual(
                (td / "runtime" / "notifications" / "subpkg" / "tool.py").read_text(),
                "VALUE = 'copied'\n",
            )

    def test_missing_package_entirely_refused_by_static_gate(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            checkout = _make_checkout(td)
            shutil.rmtree(checkout / "notifications")
            with self.assertRaises(subprocess.CalledProcessError) as ctx:
                _run_installer(checkout, td)
            self.assertIn("incomplete checkout", ctx.exception.stderr)
            self.assertIn("notifications/__init__.py", ctx.exception.stderr)
            self.assertFalse((td / "runtime").exists())

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
