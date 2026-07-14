from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TMUX_SEND = ROOT / "scripts" / "tmux-send"


FAKE_TMUX = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_TMUX_STATE"])
if state_path.exists():
    state = json.loads(state_path.read_text())
else:
    state = {"composer": "", "buffer": "", "submitted": [], "submit_keys": 0}

def save():
    state_path.write_text(json.dumps(state))

args = sys.argv[1:]
if args[:1] == ["send-keys"]:
    keys = args[3:]
    if keys == ["C-u", "C-k"]:
        state["composer"] = ""
    elif keys[:1] == ["--"]:
        state["composer"] = keys[1]
    elif keys in (["Enter"], ["-H", "1b", "5b", "31", "33", "75"]):
        state["submit_keys"] += 1
        swallow = int(os.environ.get("FAKE_TMUX_SWALLOW_SUBMITS", "0"))
        if state["submit_keys"] > swallow and state["composer"]:
            state["submitted"].append(state["composer"])
            state["composer"] = ""
    save()
    raise SystemExit(0)
if args[:1] == ["load-buffer"]:
    state["buffer"] = sys.stdin.read()
    save()
    raise SystemExit(0)
if args[:1] == ["paste-buffer"]:
    state["composer"] = state.get("buffer", "")
    save()
    raise SystemExit(0)
if args[:1] == ["capture-pane"]:
    if os.environ.get("FAKE_TMUX_TRANSCRIPT_ECHO") == "1" and state["submitted"]:
        print(f"❯ {state['submitted'][-1]}")
        print()
    print("╭────────────────────────────────────────────────────────────────────╮")
    print(f"│ ❯ {state['composer']:<62} │")
    print("╰───────────────────────────────── gpt-5.5 · conductor-codex ─╯")
    raise SystemExit(0)
raise SystemExit(f"unexpected tmux args: {args!r}")
"""


class TmuxSendTests(unittest.TestCase):
    def run_tmux_send(
        self,
        *,
        swallow_submits: int,
        transcript_echo: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], dict]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_tmux = tmp_path / "tmux"
            fake_tmux.write_text(FAKE_TMUX)
            fake_tmux.chmod(0o755)
            state_path = tmp_path / "state.json"
            env = {
                **os.environ,
                "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
                "FAKE_TMUX_STATE": str(state_path),
                "FAKE_TMUX_SWALLOW_SUBMITS": str(swallow_submits),
                "FAKE_TMUX_TRANSCRIPT_ECHO": "1" if transcript_echo else "0",
                "SUBMIT_VERIFY_SETTLE_SECS": "0.01",
            }
            result = subprocess.run(
                [str(TMUX_SEND), "local", "worker-codex", "shell retry payload"],
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
            )
            state = json.loads(state_path.read_text())
            return result, state

    def test_tmux_send_retries_when_first_submit_is_swallowed(self):
        result, state = self.run_tmux_send(swallow_submits=2)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", state["composer"])
        self.assertIn("shell retry payload", state["submitted"])
        self.assertGreater(state["submit_keys"], 2)

    def test_tmux_send_ignores_transcript_echo_above_empty_composer(self):
        result, state = self.run_tmux_send(
            swallow_submits=0,
            transcript_echo=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", state["composer"])
        self.assertIn("shell retry payload", state["submitted"])
        self.assertEqual(2, state["submit_keys"])

    def test_tmux_send_fails_when_submit_verification_stays_stranded(self):
        result, state = self.run_tmux_send(swallow_submits=999)

        self.assertNotEqual(0, result.returncode)
        self.assertEqual("shell retry payload", state["composer"])
        self.assertIn("composer still occupied", result.stderr)


if __name__ == "__main__":
    sys.exit(unittest.main())
