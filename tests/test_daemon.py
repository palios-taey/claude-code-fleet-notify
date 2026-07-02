from __future__ import annotations

import json
import sys
import threading
import time as time_module
import unittest
from types import SimpleNamespace
from unittest import mock

from hooks import _shared as shared
from notifications import daemon
from notifications.inbox import inbox_key, key_prefix, state_key
from notifications.handoff import create_explicit_handoff, explicit_ack_key, explicit_handoff_key
from tests.fakes import FakeRedis


class DaemonTests(unittest.TestCase):
    def run_daemon_once(self, redis_client, sessions, *, inject_result=True, now=1000.0):
        fake_redis_module = SimpleNamespace(Redis=lambda **kwargs: redis_client)
        with mock.patch.dict(sys.modules, {"redis": fake_redis_module}):
            with mock.patch.object(daemon, "get_local_tmux_sessions", return_value=sessions):
                with mock.patch.object(daemon, "inject_via_tmux", return_value=inject_result) as inject:
                    with mock.patch.object(daemon.time, "time", return_value=now):
                        with mock.patch.object(daemon.time, "sleep", side_effect=KeyboardInterrupt):
                            daemon.run_daemon("127.0.0.1", 6379, 1)
        return inject

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

    def test_run_daemon_writes_heartbeat_each_poll(self):
        r = FakeRedis()

        fake_redis_module = SimpleNamespace(Redis=lambda **kwargs: r)
        with mock.patch.dict(sys.modules, {"redis": fake_redis_module}):
            with mock.patch.object(daemon, "get_local_tmux_sessions", return_value=[]):
                with mock.patch.object(daemon.socket, "gethostname", return_value="notify-host"):
                    with mock.patch.object(daemon.time, "time", return_value=1234.5):
                        with mock.patch.object(daemon.time, "sleep", side_effect=KeyboardInterrupt):
                            daemon.run_daemon("127.0.0.1", 6379, 1)

        self.assertEqual("1234.500000+notify-host", r.get(state_key("_notify_daemon", "heartbeat")))

    def test_daemon_redis_client_has_socket_timeout_for_validation_scan(self):
        r = FakeRedis()
        redis_kwargs = {}

        def redis_factory(**kwargs):
            redis_kwargs.update(kwargs)
            return r

        fake_redis_module = SimpleNamespace(Redis=redis_factory)
        with mock.patch.dict(sys.modules, {"redis": fake_redis_module}):
            with mock.patch.object(daemon, "get_local_tmux_sessions", return_value=[]):
                with mock.patch.object(daemon.socket, "gethostname", return_value="notify-host"):
                    with mock.patch.object(daemon.time, "time", return_value=1234.5):
                        with mock.patch.object(daemon.time, "sleep", side_effect=KeyboardInterrupt):
                            daemon.run_daemon("127.0.0.1", 6379, 1)

        self.assertEqual(daemon.REDIS_SOCKET_TIMEOUT_SECS, redis_kwargs.get("socket_timeout"))
        self.assertEqual(daemon.REDIS_SOCKET_TIMEOUT_SECS, redis_kwargs.get("socket_connect_timeout"))
        self.assertLess(daemon.REDIS_SOCKET_TIMEOUT_SECS, daemon.HANDOFF_VALIDATION_TIMEOUT_SECS)

    def test_handoff_validation_timeout_does_not_wedge_delivery(self):
        class BlockingHandoffScanRedis(FakeRedis):
            def __init__(self):
                super().__init__()
                self.validation_started = threading.Event()
                self.release_validation = threading.Event()
                self.validation_done = threading.Event()

            def scan_iter(self, match=None, count=None):
                if match == f"{key_prefix()}:handoff:*":
                    self.validation_started.set()
                    self.release_validation.wait(1.0)
                    self.validation_done.set()
                    if False:
                        yield None
                    return
                yield from super().scan_iter(match=match, count=count)

        r = BlockingHandoffScanRedis()
        r.set(state_key("wedged", "idle"), "1")
        r.lpush(inbox_key("wedged"), json.dumps({"from": "sender", "type": "message", "body": "queued"}))

        fake_redis_module = SimpleNamespace(Redis=lambda **kwargs: r)
        started_at = time_module.monotonic()
        try:
            with mock.patch.object(daemon, "HANDOFF_VALIDATION_TIMEOUT_SECS", 0.05):
                with mock.patch.dict(sys.modules, {"redis": fake_redis_module}):
                    with mock.patch.object(daemon, "get_local_tmux_sessions", return_value=["wedged"]):
                        with mock.patch.object(daemon, "inject_via_tmux", return_value=True) as inject:
                            with mock.patch.object(daemon.time, "time", return_value=1234.5):
                                with mock.patch.object(daemon.time, "sleep", side_effect=KeyboardInterrupt):
                                    daemon.run_daemon("127.0.0.1", 6379, 1)
        finally:
            r.release_validation.set()
            r.validation_done.wait(1.0)
            time_module.sleep(0.02)

        elapsed = time_module.monotonic() - started_at
        self.assertTrue(r.validation_started.is_set())
        self.assertLess(elapsed, 0.5)
        inject.assert_called_once()
        self.assertEqual("wedged", inject.call_args.args[0])
        self.assertEqual("1234.500000+%s" % daemon.socket.gethostname(),
                         r.get(state_key("_notify_daemon", "heartbeat")))

    def test_handoff_validation_job_stays_nonblocking_while_running(self):
        r = FakeRedis()
        started = threading.Event()
        release = threading.Event()
        calls = []
        job = None

        def blocking_validation(redis_client, *, prefix, timeout_sec):
            calls.append((redis_client, prefix, timeout_sec))
            started.set()
            release.wait(1.0)
            return 1

        try:
            with mock.patch.object(daemon, "validate_handoff_activation", side_effect=blocking_validation):
                job = daemon._advance_handoff_validation_job(
                    None,
                    r,
                    prefix=key_prefix(),
                    timeout_sec=0.02,
                )
                self.assertTrue(started.wait(0.5))
                time_module.sleep(0.03)

                latencies = []
                for _ in range(5):
                    started_at = time_module.monotonic()
                    next_job = daemon._advance_handoff_validation_job(
                        job,
                        r,
                        prefix=key_prefix(),
                        timeout_sec=0.02,
                    )
                    latencies.append(time_module.monotonic() - started_at)
                    self.assertIs(next_job, job)

                self.assertEqual(1, len(calls))
                self.assertTrue(job.warned)
                self.assertLess(max(latencies), 0.25)
        finally:
            release.set()
            if job is not None:
                self.assertTrue(job.done.wait(1.0))

    def test_handoff_validation_job_consumes_logs_and_restarts(self):
        r = FakeRedis()
        calls = []

        def validation(redis_client, *, prefix, timeout_sec):
            del redis_client, prefix, timeout_sec
            calls.append(len(calls) + 1)
            if calls[-1] == 1:
                raise RuntimeError("validation failed")
            return calls[-1]

        with mock.patch.object(daemon, "validate_handoff_activation", side_effect=validation):
            with mock.patch.object(daemon.logger, "error") as log_error:
                job_one = daemon._advance_handoff_validation_job(
                    None,
                    r,
                    prefix=key_prefix(),
                    timeout_sec=0.02,
                )
                self.assertTrue(job_one.done.wait(1.0))

                job_two = daemon._advance_handoff_validation_job(
                    job_one,
                    r,
                    prefix=key_prefix(),
                    timeout_sec=0.02,
                )
                self.assertIsNot(job_two, job_one)
                self.assertTrue(job_two.done.wait(1.0))
                self.assertEqual(2, job_two.updated)
                self.assertEqual(2, len(calls))
                log_error.assert_called_once()
                self.assertIn("handoff activation validation failed", log_error.call_args.args[0])
                self.assertIsInstance(log_error.call_args.args[1], RuntimeError)

                job_three = daemon._advance_handoff_validation_job(
                    job_two,
                    r,
                    prefix=key_prefix(),
                    timeout_sec=0.02,
                )
                self.assertIsNot(job_three, job_two)
                self.assertTrue(job_three.done.wait(1.0))
                self.assertEqual(3, job_three.updated)
                self.assertEqual(3, len(calls))

    def test_idle_flagged_session_injects_even_with_tool_running_and_activity_markers(self):
        r = FakeRedis()
        r.set(state_key("idle-session", "idle"), "1")
        r.set(state_key("idle-session", "tool_running"), "1")
        r.set(state_key("idle-session", "last_activity"), "1999")
        r.lpush(inbox_key("idle-session"), json.dumps({
            "from": "sender",
            "type": "message",
            "body": "body mentioning Esc to interrupt",
        }))

        inject = self.run_daemon_once(r, ["idle-session"], now=2000.0)

        inject.assert_called_once()
        self.assertEqual("idle-session", inject.call_args.args[0])
        self.assertEqual(1, r.llen(inbox_key("idle-session")))

    def test_run_daemon_does_not_inject_idle_absent_sessions_regardless_of_stale_activity(self):
        r = FakeRedis()
        sessions = [
            "fail-open-stop",
            "redis-none-stop",
            "decision-block-stop",
            "action-stop-exception",
            "hook-never-fired",
        ]
        for node_id in sessions:
            r.set(state_key(node_id, "last_activity"), "1000")
            r.lpush(inbox_key(node_id), json.dumps({"from": "sender", "type": "message", "body": node_id}))

        inject = self.run_daemon_once(r, sessions, now=2000.0)

        inject.assert_not_called()
        for node_id in sessions:
            self.assertFalse(r.exists(state_key(node_id, "idle")))
            self.assertEqual(1, r.llen(inbox_key(node_id)))

    def test_run_daemon_does_not_inject_idle_absent_regardless_of_tool_running_or_pane_prose(self):
        r = FakeRedis()
        r.set(state_key("tool-running", "tool_running"), "1")
        r.set(state_key("tool-running", "last_activity"), "900")
        r.lpush(inbox_key("tool-running"), json.dumps({"from": "sender", "type": "message", "body": "tool"}))
        r.set(state_key("recent-activity", "last_activity"), "1980")
        r.lpush(inbox_key("recent-activity"), json.dumps({"from": "sender", "type": "message", "body": "recent"}))
        r.set(state_key("pane-marker-prose", "last_activity"), "1000")
        r.lpush(inbox_key("pane-marker-prose"), json.dumps({
            "from": "sender",
            "type": "message",
            "body": "operator notes said esc to interrupt in prose",
        }))

        inject = self.run_daemon_once(
            r,
            ["tool-running", "recent-activity", "pane-marker-prose"],
            now=2000.0,
        )

        inject.assert_not_called()

    def test_idle_absent_activity_marker_waits_for_post_tool_drain(self):
        r = FakeRedis()
        r.set(state_key("long-tool", "last_activity"), "1000")
        r.lpush(inbox_key("long-tool"), json.dumps({"from": "sender", "type": "message", "body": "queued"}))

        inject = self.run_daemon_once(r, ["long-tool"], now=2000.0)

        inject.assert_not_called()
        self.assertEqual(1, r.llen(inbox_key("long-tool")))

        context = shared.action_post_tool(r, "long-tool", tool_name="Bash")

        self.assertIn("queued", context)
        self.assertEqual(0, r.llen(inbox_key("long-tool")))

    def test_usage_limit_banner_reconciles_parent_idle_before_inject(self):
        r = FakeRedis()
        r.set(state_key("gatekeeper", "last_activity"), "1000")
        r.lpush(inbox_key("gatekeeper"), json.dumps({"from": "sender", "type": "message", "body": "queued"}))

        with mock.patch.object(
            daemon,
            "_tmux_pane_tail",
            return_value="You've hit your session limit · resets 2:10am (Africa/Abidjan)",
        ):
            inject = self.run_daemon_once(r, ["gatekeeper"], now=2500.0)

        inject.assert_called_once()
        self.assertEqual("gatekeeper", inject.call_args.args[0])
        self.assertTrue(r.exists(state_key("gatekeeper", "idle")))
        self.assertEqual(1, r.llen(inbox_key("gatekeeper")))

    def test_usage_limit_reconcile_matches_reached_weekly_and_usage_variants(self):
        banners = (
            "You've reached your weekly limit · resets 2:10am (Africa/Abidjan)",
            "You have reached your weekly limit · resets 2:10am (Africa/Abidjan)",
            "You've reached your usage limit · resets 2:10am (Africa/Abidjan)",
            "You have reached your usage limit · resets 2:10am (Africa/Abidjan)",
        )
        for idx, banner in enumerate(banners):
            with self.subTest(banner=banner):
                node_id = f"gatekeeper-{idx}"
                r = FakeRedis()
                r.set(state_key(node_id, "last_activity"), "1000")
                r.lpush(inbox_key(node_id), json.dumps({"from": "sender", "type": "message", "body": "queued"}))

                with mock.patch.object(daemon, "_tmux_pane_tail", return_value=f"Claude Code\n{banner}"):
                    inject = self.run_daemon_once(r, [node_id], now=2500.0)

                inject.assert_called_once()
                self.assertEqual(node_id, inject.call_args.args[0])
                self.assertTrue(r.exists(state_key(node_id, "idle")))
                self.assertEqual(1, r.llen(inbox_key(node_id)))

    def test_usage_limit_reconcile_ignores_stale_banner_above_visible_resting_region(self):
        r = FakeRedis()
        r.set(state_key("gatekeeper", "last_activity"), "1000")
        r.lpush(inbox_key("gatekeeper"), json.dumps({"from": "sender", "type": "message", "body": "queued"}))
        pane_tail = "\n".join((
            "You've hit your session limit · resets 2:10am (Africa/Abidjan)",
            "$ python long_running_job.py",
            "processing task batch",
            "writing output",
            "Claude Code ready at prompt",
        ))

        with mock.patch.object(daemon, "_tmux_pane_tail", return_value=pane_tail):
            inject = self.run_daemon_once(r, ["gatekeeper"], now=2500.0)

        inject.assert_not_called()
        self.assertFalse(r.exists(state_key("gatekeeper", "idle")))
        self.assertEqual(1, r.llen(inbox_key("gatekeeper")))

    def test_usage_limit_reconcile_does_not_restore_generic_stale_idle_absence(self):
        r = FakeRedis()
        r.set(state_key("gatekeeper", "last_activity"), "1000")
        r.lpush(inbox_key("gatekeeper"), json.dumps({"from": "sender", "type": "message", "body": "queued"}))

        with mock.patch.object(daemon, "_tmux_pane_tail", return_value="Claude Code ready at prompt"):
            inject = self.run_daemon_once(r, ["gatekeeper"], now=2500.0)

        inject.assert_not_called()
        self.assertFalse(r.exists(state_key("gatekeeper", "idle")))
        self.assertEqual(1, r.llen(inbox_key("gatekeeper")))

    def test_transient_not_your_usage_limit_does_not_reconcile_idle(self):
        r = FakeRedis()
        r.set(state_key("gatekeeper", "last_activity"), "1000")
        r.lpush(inbox_key("gatekeeper"), json.dumps({"from": "sender", "type": "message", "body": "queued"}))

        with mock.patch.object(
            daemon,
            "_tmux_pane_tail",
            return_value="API Error: Server is temporarily limiting requests (not your usage limit)",
        ):
            inject = self.run_daemon_once(r, ["gatekeeper"], now=2500.0)

        inject.assert_not_called()
        self.assertFalse(r.exists(state_key("gatekeeper", "idle")))

    def test_usage_limit_reconcile_respects_tool_running(self):
        r = FakeRedis()
        r.set(state_key("gatekeeper", "last_activity"), "1000")
        r.set(state_key("gatekeeper", "tool_running"), "1")
        r.lpush(inbox_key("gatekeeper"), json.dumps({"from": "sender", "type": "message", "body": "queued"}))

        with mock.patch.object(
            daemon,
            "_tmux_pane_tail",
            return_value="You've hit your session limit · resets 2:10am (Africa/Abidjan)",
        ):
            inject = self.run_daemon_once(r, ["gatekeeper"], now=2500.0)

        inject.assert_not_called()
        self.assertFalse(r.exists(state_key("gatekeeper", "idle")))
        self.assertEqual(1, r.llen(inbox_key("gatekeeper")))

    def test_idle_flag_is_authoritative_even_when_tool_running_exists(self):
        r = FakeRedis()
        r.set(state_key("mid-tool", "idle"), "1")
        r.set(state_key("mid-tool", "tool_running"), "1")
        r.lpush(inbox_key("mid-tool"), json.dumps({"from": "sender", "type": "message", "body": "queued"}))

        inject = self.run_daemon_once(r, ["mid-tool"], now=2000.0)

        inject.assert_called_once()
        self.assertEqual("mid-tool", inject.call_args.args[0])
        self.assertEqual(1, r.llen(inbox_key("mid-tool")))

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

    def test_repeated_pointer_inject_failures_notify_supervisor_once(self):
        r = FakeRedis()
        r.set(state_key("worker-codex", "parent"), "conductor")
        r.set(state_key("worker-codex", "idle"), "1")
        r.lpush(inbox_key("worker-codex"), json.dumps({"from": "sender", "type": "message", "body": "body"}))

        for now in (1000.0, 1002.0, 1004.0, 1006.0):
            with mock.patch.object(daemon, "POINTER_INJECT_BACKOFF_SECS", 1):
                self.run_daemon_once(r, ["worker-codex"], inject_result=False, now=now)

        notices = r.decoded_list(inbox_key("conductor"))
        self.assertEqual(1, len(notices))
        self.assertEqual("inject_failure", notices[0]["type"])
        self.assertEqual("worker-codex", notices[0]["from"])
        self.assertEqual(3, notices[0]["failure_count"])
        self.assertEqual("4", r.get(state_key("worker-codex", "pointer_inject_fail_count")))

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

    def test_dispatch_activation_failure_notifies_dispatcher(self):
        r = FakeRedis()
        with mock.patch.object(daemon.time, "time", return_value=1000):
            payload = create_explicit_handoff(
                r,
                prefix="taey",
                dispatcher_session_id="conductor",
                target_session_id="worker-codex",
                body="dispatch body",
                msg_type="command",
                priority="normal",
                dispatcher_task_id="task-59",
            )
        fake_redis_module = SimpleNamespace(Redis=lambda **kwargs: r)
        with mock.patch.dict(sys.modules, {"redis": fake_redis_module}):
            with mock.patch.object(daemon, "get_local_tmux_sessions", return_value=[]):
                with mock.patch.object(daemon.time, "time", return_value=1061):
                    with mock.patch.object(daemon.time, "sleep", side_effect=KeyboardInterrupt):
                        daemon.run_daemon("127.0.0.1", 6379, 1)

        record = json.loads(r.get(explicit_handoff_key("taey", "conductor", payload["msg_id"])))
        msg = r.decoded_list(inbox_key("conductor"))[0]
        self.assertEqual("failed", record["activation_state"])
        self.assertEqual("dispatch_activation_failed", msg["type"])
        self.assertEqual("worker-codex", msg["target_session_id"])
        self.assertEqual("task-59", msg["dispatcher_task_id"])

    def test_dispatch_activation_heartbeat_marks_record_activated(self):
        r = FakeRedis()
        r.set(state_key("worker-codex", "last_tool_activity"), "900")
        with mock.patch.object(daemon.time, "time", return_value=1000):
            payload = create_explicit_handoff(
                r,
                prefix="taey",
                dispatcher_session_id="conductor",
                target_session_id="worker-codex",
                body="dispatch body",
                msg_type="command",
                priority="normal",
                dispatcher_task_id="task-59",
            )
        r.set(state_key("worker-codex", "last_tool_activity"), "1005")
        fake_redis_module = SimpleNamespace(Redis=lambda **kwargs: r)
        with mock.patch.dict(sys.modules, {"redis": fake_redis_module}):
            with mock.patch.object(daemon, "get_local_tmux_sessions", return_value=[]):
                with mock.patch.object(daemon.time, "time", return_value=1006):
                    with mock.patch.object(daemon.time, "sleep", side_effect=KeyboardInterrupt):
                        daemon.run_daemon("127.0.0.1", 6379, 1)

        record = json.loads(r.get(explicit_handoff_key("taey", "conductor", payload["msg_id"])))
        self.assertEqual("activated", record["activation_state"])
        self.assertEqual("heartbeat", record["activation_source"])
        self.assertEqual(0, r.llen(inbox_key("conductor")))

    def test_dispatch_activation_ack_marks_record_activated(self):
        r = FakeRedis()
        with mock.patch.object(daemon.time, "time", return_value=1000):
            payload = create_explicit_handoff(
                r,
                prefix="taey",
                dispatcher_session_id="conductor",
                target_session_id="worker-codex",
                body="dispatch body",
                msg_type="command",
                priority="normal",
                dispatcher_task_id="task-59",
            )
        r.set(explicit_ack_key("taey", "conductor", "worker-codex", payload["msg_id"]), "{}")
        fake_redis_module = SimpleNamespace(Redis=lambda **kwargs: r)
        with mock.patch.dict(sys.modules, {"redis": fake_redis_module}):
            with mock.patch.object(daemon, "get_local_tmux_sessions", return_value=[]):
                with mock.patch.object(daemon.time, "time", return_value=1006):
                    with mock.patch.object(daemon.time, "sleep", side_effect=KeyboardInterrupt):
                        daemon.run_daemon("127.0.0.1", 6379, 1)

        record = json.loads(r.get(explicit_handoff_key("taey", "conductor", payload["msg_id"])))
        self.assertEqual("activated", record["activation_state"])
        self.assertEqual("ack", record["activation_source"])

    def test_dispatch_activation_current_task_bound_after_handoff_marks_record_activated(self):
        r = FakeRedis()
        with mock.patch.object(daemon.time, "time", return_value=1000):
            payload = create_explicit_handoff(
                r,
                prefix="taey",
                dispatcher_session_id="conductor",
                target_session_id="worker-codex",
                body="dispatch body",
                msg_type="command",
                priority="normal",
                dispatcher_task_id="task-59",
            )
        r.set(state_key("worker-codex", "current_task"), json.dumps({
            "task_id": "task-59",
            "started_at": "1001",
        }))
        fake_redis_module = SimpleNamespace(Redis=lambda **kwargs: r)
        with mock.patch.dict(sys.modules, {"redis": fake_redis_module}):
            with mock.patch.object(daemon, "get_local_tmux_sessions", return_value=[]):
                with mock.patch.object(daemon.time, "time", return_value=1006):
                    with mock.patch.object(daemon.time, "sleep", side_effect=KeyboardInterrupt):
                        daemon.run_daemon("127.0.0.1", 6379, 1)

        record = json.loads(r.get(explicit_handoff_key("taey", "conductor", payload["msg_id"])))
        self.assertEqual("activated", record["activation_state"])
        self.assertEqual("current_task", record["activation_source"])

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

    def test_pointer_inject_backoff_suppresses_repeat_for_wedged_session(self):
        # Regression for the 3s keystroke-hammer: a wedged session (idle stuck
        # on, inbox unchanged across polls) must be injected at most ONCE per
        # backoff window, not on every poll.
        r = FakeRedis()
        r.set(state_key("wedged", "idle"), "1")
        r.lpush(inbox_key("wedged"), json.dumps({"from": "s", "type": "message", "body": "x"}))
        fake_redis_module = SimpleNamespace(Redis=lambda **kwargs: r)
        sleeps = {"n": 0}

        def stop_after_five(_):
            sleeps["n"] += 1
            if sleeps["n"] >= 5:
                raise KeyboardInterrupt

        with mock.patch.dict(sys.modules, {"redis": fake_redis_module}):
            with mock.patch.object(daemon, "get_local_tmux_sessions", return_value=["wedged"]):
                with mock.patch.object(daemon, "inject_via_tmux", return_value=True) as inject:
                    with mock.patch.object(daemon.time, "time", return_value=1000.0):
                        with mock.patch.object(daemon.time, "sleep", side_effect=stop_after_five):
                            daemon.run_daemon("127.0.0.1", 6379, 1)

        # 5 polls, same time, unchanged inbox -> exactly one inject (backoff held)
        inject.assert_called_once()

    def test_pointer_inject_backoff_new_message_injects_promptly(self):
        # A NEW message changes the inbox signature and must inject promptly even
        # while a recent backoff stamp exists (no starvation of new messages).
        import hashlib

        r = FakeRedis()
        r.set(state_key("wedged", "idle"), "1")
        r.lpush(inbox_key("wedged"), json.dumps({"from": "s", "type": "message", "body": "first"}))
        sig_one = hashlib.sha1(
            b"\n".join(s.encode() for s in r.lrange(inbox_key("wedged"), 0, -1))
        ).hexdigest()
        # recent backoff stamp for the CURRENT (one-message) inbox state
        r.set(state_key("wedged", "pointer_inject_backoff"),
              json.dumps({"sig": sig_one, "ts": 1000.0}))
        # a NEW message arrives -> signature changes
        r.lpush(inbox_key("wedged"), json.dumps({"from": "s", "type": "message", "body": "NEW"}))
        fake_redis_module = SimpleNamespace(Redis=lambda **kwargs: r)

        with mock.patch.dict(sys.modules, {"redis": fake_redis_module}):
            with mock.patch.object(daemon, "get_local_tmux_sessions", return_value=["wedged"]):
                with mock.patch.object(daemon, "inject_via_tmux", return_value=True) as inject:
                    with mock.patch.object(daemon.time, "time", return_value=1001.0):
                        with mock.patch.object(daemon.time, "sleep", side_effect=KeyboardInterrupt):
                            daemon.run_daemon("127.0.0.1", 6379, 1)

        # within the backoff window BUT signature changed -> injects promptly
        inject.assert_called_once()


if __name__ == "__main__":
    unittest.main()
