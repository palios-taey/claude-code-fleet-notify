from __future__ import annotations

import json
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from notifications import daemon
from notifications.inbox import inbox_key, state_key
from tests.fakes import FakeRedis


class DaemonTests(unittest.TestCase):
    def test_build_pointer_summary_does_not_pop_messages(self):
        r = FakeRedis()
        r.lpush(inbox_key("session-b"), json.dumps({"from": "session-a", "type": "message", "body": "hello"}))

        summary = daemon.build_pointer_summary(r, "session-b")

        self.assertIn("You have 1 messages", summary)
        self.assertIn(inbox_key("session-b"), summary)
        self.assertEqual(1, r.llen(inbox_key("session-b")))

    def test_run_daemon_injects_only_idle_sessions_with_messages(self):
        r = FakeRedis()
        r.set(state_key("idle-session", "idle"), "1")
        r.lpush(inbox_key("idle-session"), json.dumps({"from": "sender", "type": "message", "body": "body"}))
        r.lpush(inbox_key("busy-session"), json.dumps({"from": "sender", "type": "message", "body": "body"}))

        fake_redis_module = SimpleNamespace(Redis=lambda **kwargs: r)
        with mock.patch.dict(sys.modules, {"redis": fake_redis_module}):
            with mock.patch.object(daemon, "get_local_tmux_sessions", return_value=["idle-session", "busy-session"]):
                with mock.patch.object(daemon, "inject_via_tmux", return_value=True) as inject:
                    with mock.patch.object(daemon.time, "sleep", side_effect=KeyboardInterrupt):
                        daemon.run_daemon("127.0.0.1", 6379, 1)

        inject.assert_called_once()
        self.assertEqual("idle-session", inject.call_args.args[0])
        self.assertEqual(1, r.llen(inbox_key("idle-session")))
        self.assertTrue(r.exists(state_key("idle-session", "idle")))

    def test_failed_injection_leaves_message_and_idle_for_retry(self):
        r = FakeRedis()
        r.set(state_key("session-b", "idle"), "1")
        r.lpush(inbox_key("session-b"), json.dumps({"from": "sender", "type": "message", "body": "body"}))

        fake_redis_module = SimpleNamespace(Redis=lambda **kwargs: r)
        with mock.patch.dict(sys.modules, {"redis": fake_redis_module}):
            with mock.patch.object(daemon, "get_local_tmux_sessions", return_value=["session-b"]):
                with mock.patch.object(daemon, "inject_via_tmux", return_value=False):
                    with mock.patch.object(daemon.time, "sleep", side_effect=KeyboardInterrupt):
                        daemon.run_daemon("127.0.0.1", 6379, 1)

        self.assertEqual(1, r.llen(inbox_key("session-b")))
        self.assertTrue(r.exists(state_key("session-b", "idle")))


if __name__ == "__main__":
    unittest.main()
