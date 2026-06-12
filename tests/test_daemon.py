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

    def test_handoff_injection_success_records_poll_signal(self):
        r = FakeRedis()
        r.set(state_key("worker-codex", "idle"), "1")
        payload = {
            "from": "conductor-codex",
            "type": "command",
            "body": "handoff body",
            "msg_id": "123e4567-e89b-12d3-a456-426614174000",
            "handoff_kind": "explicit_handoff",
            "dispatcher_session_id": "conductor-codex",
            "target_session_id": "worker-codex",
            "message_hash": "hash-1",
        }
        record_key = "taey:handoff:conductor-codex:123e4567-e89b-12d3-a456-426614174000"
        r.lpush(inbox_key("worker-codex"), json.dumps(payload))
        r.set(record_key, json.dumps({
            "kind": "explicit_handoff",
            "dispatcher_session_id": "conductor-codex",
            "target_session_id": "worker-codex",
            "dispatcher_task_id": "task-123",
            "msg_id": payload["msg_id"],
            "message_hash": "hash-1",
            "created_at": 1.0,
            "ack_deadline_at": 9999999999.0,
            "ack_backstop_at": 9999999999.0,
            "pickup_poll_budget": 5,
            "delivery_poll_count": 0,
            "delivery_state": "queued",
        }))

        fake_redis_module = SimpleNamespace(Redis=lambda **kwargs: r)
        with mock.patch.dict(sys.modules, {"redis": fake_redis_module}):
            with mock.patch.object(daemon, "get_local_tmux_sessions", return_value=["worker-codex"]):
                with mock.patch.object(daemon, "inject_via_tmux", return_value=True):
                    with mock.patch.object(daemon.time, "sleep", side_effect=KeyboardInterrupt):
                        daemon.run_daemon("127.0.0.1", 6379, 1)

        record = json.loads(r.get(record_key))
        self.assertEqual("injected_waiting_ack", record["delivery_state"])
        self.assertEqual("inject_ok", record["last_delivery_signal"])
        self.assertEqual(1, record["delivery_poll_count"])

    def test_stale_peer_with_current_task_notifies_dispatcher_even_without_idle(self):
        r = FakeRedis()
        r.set(state_key("worker-codex", "parent"), "conductor")
        r.set(state_key("worker-codex", "last_tool_activity"), "1000")
        r.set(state_key("worker-codex", "current_task"), json.dumps({
            "task_id": "task-59",
            "description": "stalled quota menu",
            "supervisor": "conductor",
            "started_at": "900",
        }))

        fake_redis_module = SimpleNamespace(Redis=lambda **kwargs: r)
        with mock.patch.dict(sys.modules, {"redis": fake_redis_module}):
            with mock.patch.object(daemon, "get_local_tmux_sessions", return_value=["worker-codex"]):
                with mock.patch.object(daemon.time, "time", return_value=1400):
                    with mock.patch.object(daemon.time, "sleep", side_effect=KeyboardInterrupt):
                        daemon.run_daemon("127.0.0.1", 6379, 1)

        msg = r.decoded_list(inbox_key("conductor"))[0]
        self.assertEqual("peer_idle", msg["type"])
        self.assertEqual("task-59", msg["task_id"])
        self.assertEqual("stale_last_tool_activity", msg["backstop"])
        self.assertEqual(400, msg["inactive_for_sec"])

    def test_missing_tmux_marks_local_handoff_not_deliverable(self):
        r = FakeRedis()
        payload = {
            "from": "conductor-codex",
            "type": "command",
            "body": "handoff body",
            "msg_id": "223e4567-e89b-12d3-a456-426614174000",
            "handoff_kind": "explicit_handoff",
            "dispatcher_session_id": "conductor-codex",
            "target_session_id": "worker-codex",
            "message_hash": "hash-2",
        }
        record_key = "taey:handoff:conductor-codex:223e4567-e89b-12d3-a456-426614174000"
        r.set("taey:worker-codex:machine", "test-host")
        r.lpush(inbox_key("worker-codex"), json.dumps(payload))
        r.set(record_key, json.dumps({
            "kind": "explicit_handoff",
            "dispatcher_session_id": "conductor-codex",
            "target_session_id": "worker-codex",
            "dispatcher_task_id": "task-456",
            "msg_id": payload["msg_id"],
            "message_hash": "hash-2",
            "created_at": 1.0,
            "ack_deadline_at": 9999999999.0,
            "ack_backstop_at": 9999999999.0,
            "pickup_poll_budget": 5,
            "delivery_poll_count": 0,
            "delivery_state": "queued",
        }))

        fake_redis_module = SimpleNamespace(Redis=lambda **kwargs: r)
        with mock.patch.dict(sys.modules, {"redis": fake_redis_module}):
            with mock.patch.object(daemon, "get_local_tmux_sessions", return_value=[]):
                with mock.patch.object(daemon.socket, "gethostname", return_value="test-host"):
                    with mock.patch.object(daemon.time, "sleep", side_effect=KeyboardInterrupt):
                        daemon.run_daemon("127.0.0.1", 6379, 1)

        record = json.loads(r.get(record_key))
        self.assertEqual("not_deliverable", record["delivery_state"])
        self.assertEqual("tmux_missing", record["delivery_failure_reason"])


if __name__ == "__main__":
    unittest.main()
