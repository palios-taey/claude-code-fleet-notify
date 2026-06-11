"""Every CLI must work when installed as a /usr/local/bin SYMLINK.

2026-06-11: taey-handoff — the receipt-validation CLI — raised
ModuleNotFoundError('notifications') on every installed invocation because it
had no repo-root bootstrap and sys.path[0] is the symlink's directory, not the
repo. It had never been installed before, so nobody had ever hit it: the
handoff-validation layer was unusable from day one. taey-notify --handoff had
the same latent break (lazy import). This test invokes every scripts/taey-*
THROUGH a symlink and asserts no import-level failure can ship again.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = sorted((REPO / "scripts").glob("taey-*"))


def _run_via_symlink(script: Path, args: list[str], tmp: Path) -> subprocess.CompletedProcess:
    link = tmp / script.name
    if not link.exists():
        link.symlink_to(script)
    return subprocess.run([str(link), *args], capture_output=True, text=True, timeout=15)


class SymlinkInstallation(unittest.TestCase):
    def test_help_via_symlink_no_import_errors(self):
        with tempfile.TemporaryDirectory() as td:
            for script in SCRIPTS:
                with self.subTest(cli=script.name):
                    proc = _run_via_symlink(script, ["--help"], Path(td))
                    self.assertNotIn("ModuleNotFoundError", proc.stderr)
                    self.assertNotIn("ImportError", proc.stderr)
                    self.assertEqual(proc.returncode, 0,
                                     f"{script.name} --help failed: {proc.stderr[:200]}")

    def test_handoff_import_path_via_symlink(self):
        """Reach the actual repo-package import (the 2026-06-11 break), pointed
        at a dead Redis port so it fails on CONNECTION, never on IMPORT."""
        with tempfile.TemporaryDirectory() as td:
            proc = _run_via_symlink(
                REPO / "scripts" / "taey-handoff",
                ["sometarget", "ping", "--redis-host", "127.0.0.1", "--redis-port", "1"],
                Path(td),
            )
            self.assertNotIn("ModuleNotFoundError", proc.stderr,
                             f"handoff import broken via symlink: {proc.stderr[:300]}")

    def test_notify_handoff_lazy_import_via_symlink(self):
        env = dict(os.environ, REDIS_HOST="127.0.0.1", REDIS_PORT="1")
        with tempfile.TemporaryDirectory() as td:
            link = Path(td) / "taey-notify"
            link.symlink_to(REPO / "scripts" / "taey-notify")
            proc = subprocess.run(
                [str(link), "sometarget", "ping", "--handoff"],
                capture_output=True, text=True, timeout=15, env=env,
            )
            self.assertNotIn("ModuleNotFoundError", proc.stderr,
                             f"notify --handoff lazy import broken: {proc.stderr[:300]}")


if __name__ == "__main__":
    unittest.main()
