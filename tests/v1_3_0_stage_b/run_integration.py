#!/usr/bin/env python3
from __future__ import annotations

import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hooks import _shared as shared  # noqa: E402
from notifications.inbox import state_key  # noqa: E402
from tests.fakes import FakeRedis  # noqa: E402


def record(lines: list[str], label: str, ok: bool, evidence: str) -> None:
    line = f"{'PASS' if ok else 'FAIL'} {label} {evidence}"
    lines.append(line)
    print(line, flush=True)


def test_allow_stop() -> tuple[bool, str]:
    r = FakeRedis()
    with mock.patch.object(shared, "_resolve_supervisor", return_value="conductor"), \
         mock.patch.object(shared, "_resolve_blocked_on", return_value=None), \
         mock.patch.object(shared, "_session_pause_active", return_value=False), \
         mock.patch.object(shared, "_expire_stale_in_progress_projects", return_value=[]), \
         mock.patch.object(shared, "_get_session_supervised_projects", return_value=[
             {"id": "proj-complete", "status": "completed", "user_stop_conditions": [], "stop_reason_current": None, "stop_reason_orphaned": False},
             {"id": "proj-stop", "status": "stopped", "user_stop_conditions": [{"id": "c1", "label": "intentional", "version": 1}], "stop_reason_current": {"condition_id": "c1"}, "stop_reason_orphaned": False},
         ]):
        decision = shared._evaluate_stop_discipline(r, "conductor-codex", None)
    return decision.wake_type == shared.WAKE_ALLOW_STOP, f"wake_type={decision.wake_type}"


def test_wake_with_queue() -> tuple[bool, str]:
    r = FakeRedis()
    with mock.patch.object(shared, "_resolve_supervisor", return_value="conductor"), \
         mock.patch.object(shared, "_resolve_blocked_on", return_value=None), \
         mock.patch.object(shared, "_session_pause_active", return_value=False), \
         mock.patch.object(shared, "_expire_stale_in_progress_projects", return_value=[]), \
         mock.patch.object(shared, "_get_session_supervised_projects", return_value=[
             {"id": "proj-ready", "status": "active", "user_stop_conditions": [{"id": "launch-stop-all-ready", "label": "stop_when_all_ready_tasks_dispatched", "version": 1}], "stop_reason_current": None, "stop_reason_orphaned": False},
         ]), \
         mock.patch.object(shared, "_get_session_next_ready", return_value={
             "task_id": "task-123",
             "description": "Take next action",
             "priority": 5,
             "phase_id": "phase-1",
         }):
        decision = shared._evaluate_stop_discipline(r, "conductor-codex", None)
    ok = decision.wake_type == shared.WAKE_WITH_QUEUE and decision.task_id == "task-123"
    return ok, f"wake_type={decision.wake_type} task_id={decision.task_id}"


def test_wake_reason_required() -> tuple[bool, str]:
    r = FakeRedis()
    with mock.patch.object(shared, "_resolve_supervisor", return_value="conductor"), \
         mock.patch.object(shared, "_resolve_blocked_on", return_value=None), \
         mock.patch.object(shared, "_session_pause_active", return_value=False), \
         mock.patch.object(shared, "_expire_stale_in_progress_projects", return_value=[]), \
         mock.patch.object(shared, "_get_session_supervised_projects", return_value=[
             {"id": "proj-needs-reason", "status": "active", "user_stop_conditions": [{"id": "cond-1", "label": "wait on review", "version": 1}], "stop_reason_current": None, "stop_reason_orphaned": False},
         ]), \
         mock.patch.object(shared, "_get_session_next_ready", return_value=None):
        decision = shared._evaluate_stop_discipline(r, "conductor-codex", None)
    ok = decision.wake_type == shared.WAKE_REASON_REQUIRED and len(decision.available_conditions or []) == 1
    return ok, f"wake_type={decision.wake_type} available={len(decision.available_conditions or [])}"


def test_blocked_on_regression() -> tuple[bool, str]:
    r = FakeRedis()
    r.set(state_key("worker-codex", "current_task"), json.dumps({"task_id": "task-peer", "description": "Waiting on peer"}))
    r.set(state_key("worker-codex", "last_outcome"), json.dumps({"outcome": "unknown", "details": ""}))
    with mock.patch.object(shared, "_resolve_supervisor", return_value="conductor"), \
         mock.patch.object(shared, "_resolve_blocked_on", return_value="peer-response:foo"):
        shared._notify_supervisor_of_stop(r, "worker-codex", "conductor")
    ok = r.llen("taey:conductor:inbox") == 0
    return ok, f"inbox_len={r.llen('taey:conductor:inbox')}"


def test_engine_error_buglock() -> tuple[bool, str]:
    r = FakeRedis()
    with mock.patch.object(shared, "_resolve_supervisor", return_value="conductor"), \
         mock.patch.object(shared, "_resolve_blocked_on", return_value=None), \
         mock.patch.object(shared, "_session_pause_active", return_value=False), \
         mock.patch.object(shared, "_expire_stale_in_progress_projects", return_value=[]), \
         mock.patch.object(shared, "_get_session_supervised_projects", side_effect=RuntimeError("orch down")), \
         mock.patch.object(shared, "_open_orchestrator_bug_lock") as open_bug_lock:
        decisions = [shared._evaluate_stop_discipline(r, "worker-codex", None) for _ in range(3)]
    ok = all(d.wake_type == shared.WAKE_ENGINE_ERROR for d in decisions) and open_bug_lock.call_count == 1
    return ok, f"wake_type={decisions[-1].wake_type} buglock_calls={open_bug_lock.call_count}"


def test_heartbeat_expiry() -> tuple[bool, str]:
    r = FakeRedis()
    r.set(state_key("worker-codex", "last_activity"), "0")
    with mock.patch.object(shared, "_fetch_in_progress_projects", return_value=[
        {"project_id": "proj-stale", "task_id": "task-stale", "owner": "worker-codex", "heartbeat_exempt_secs": 0},
    ]), mock.patch.object(shared, "_mark_project_active") as mark_project_active, \
         mock.patch.object(shared.time, "time", return_value=1000):
        events = shared._expire_stale_in_progress_projects(r, "conductor")
    ok = len(events) == 1 and mark_project_active.call_count == 1
    return ok, events[0] if events else "no events"


def test_cli_prefix_match() -> tuple[bool, str]:
    captured = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/projects/proj-1":
                payload = {
                    "project": {
                        "id": "proj-1",
                        "user_stop_conditions": [
                            {"id": "f3a81234abcd", "label": "launch-stop-all-ready", "version": 1, "deprecated_at": None}
                        ],
                    }
                }
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            if self.path == "/api/projects/proj-1/stop-reason":
                length = int(self.headers.get("Content-Length", "0"))
                captured["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
                data = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = os.environ.copy()
        env.update({
            "ORCH_API_BASE": f"http://127.0.0.1:{port}",
            "TAEY_NODE_ID": "conductor",
            "PYTHONPATH": str(ROOT / "scripts"),
        })
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "taey-stop-reason"),
                "set",
                "proj-1",
                "--condition",
                "launch-stop-all",
                "--detail",
                "intentional stop",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        server.shutdown()
    ok = result.returncode == 0 and captured.get("body", {}).get("condition_id") == "f3a81234abcd"
    return ok, f"returncode={result.returncode} condition_id={captured.get('body', {}).get('condition_id')}"


def main() -> int:
    lines: list[str] = []
    tests = [
        ("allow_stop", test_allow_stop),
        ("wake_with_queue", test_wake_with_queue),
        ("wake_reason_required", test_wake_reason_required),
        ("blocked_on_regression", test_blocked_on_regression),
        ("engine_error_buglock", test_engine_error_buglock),
        ("heartbeat_expiry", test_heartbeat_expiry),
        ("cli_prefix_match", test_cli_prefix_match),
    ]
    failures = 0
    for label, fn in tests:
        try:
            ok, evidence = fn()
        except Exception as exc:
            ok, evidence = False, repr(exc)
        record(lines, label, ok, evidence)
        failures += 0 if ok else 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
