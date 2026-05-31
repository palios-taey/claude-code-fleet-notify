#!/usr/bin/env python3
from __future__ import annotations

import http.server
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
ORCH_ROOT = Path("/path/to/repo")
os.environ["ORCH_NEO4J_URI"] = os.environ.get("STAGE_A_TEST_NEO4J_URI", "bolt://127.0.0.1:7691")
os.environ.setdefault("ORCH_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("ORCH_REDIS_PORT", "6379")
os.environ.pop("ORCH_NEO4J_USER", None)
os.environ.pop("ORCH_NEO4J_PASS", None)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ORCH_ROOT))

from hooks import _shared as shared  # noqa: E402
import lib.config as orch_config_module  # noqa: E402
from lib.config import OrchConfig, get_neo4j_driver  # noqa: E402
from lib.orch_schema import create_phase, create_project, create_task  # noqa: E402
from notifications.inbox import state_key  # noqa: E402
from tests.fakes import FakeRedis  # noqa: E402

REAL_CFG = OrchConfig()
REAL_CFG.neo4j_db = "neo4j"


def record(lines: list[str], label: str, ok: bool, evidence: str) -> None:
    line = f"{'PASS' if ok else 'FAIL'} {label} {evidence}"
    lines.append(line)
    print(line, flush=True)


def _reset_orch_driver() -> None:
    driver = getattr(orch_config_module, "_neo4j_driver", None)
    if driver is not None:
        try:
            driver.close()
        except Exception:
            pass
    orch_config_module._neo4j_driver = None


def _cleanup_real_graph(prefix: str) -> None:
    _reset_orch_driver()
    driver = get_neo4j_driver(REAL_CFG)
    with driver.session(database=REAL_CFG.neo4j_db) as session:
        session.run("MATCH (t:OrchTask) WHERE t.id STARTS WITH $prefix DETACH DELETE t", prefix=prefix)
        session.run("MATCH (ph:OrchPhase) WHERE ph.id STARTS WITH $prefix DETACH DELETE ph", prefix=prefix)
        session.run("MATCH (p:OrchProject) WHERE p.id STARTS WITH $prefix DETACH DELETE p", prefix=prefix)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_http(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except urllib.error.HTTPError as exc:
            if exc.code in {200, 404}:
                return
            last_error = exc
        except Exception as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"server did not become ready: {last_error}")


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


def test_pause_allows_stop() -> tuple[bool, str]:
    r = FakeRedis()
    r.set(state_key("conductor", "pause"), "1")
    with mock.patch.object(shared, "_resolve_supervisor", return_value="conductor"), \
         mock.patch.object(shared, "_resolve_blocked_on", return_value=None), \
         mock.patch.object(shared, "_expire_stale_in_progress_projects", return_value=[]), \
         mock.patch.object(shared, "_get_session_supervised_projects", return_value=[
             {"id": "proj-paused", "status": "active", "user_stop_conditions": [], "stop_reason_current": None, "stop_reason_orphaned": False},
         ]):
        decision = shared._evaluate_stop_discipline(r, "worker-codex", None)
    ok = decision.wake_type == shared.WAKE_ALLOW_STOP
    return ok, f"wake_type={decision.wake_type}"


def test_blocked_on_done_clear() -> tuple[bool, str]:
    r = FakeRedis()
    r.set(state_key("worker-codex", "current_task"), json.dumps({"task_id": "task-done", "description": "Done but blocked"}))
    r.set(state_key("worker-codex", "last_outcome"), json.dumps({"outcome": "done", "details": ""}))
    with mock.patch.object(shared, "_resolve_supervisor", return_value="conductor"), \
         mock.patch.object(shared, "_resolve_blocked_on", return_value="peer-response:done"):
        shared._notify_supervisor_of_stop(r, "worker-codex", "conductor")
    cleared = r.get(state_key("worker-codex", "current_task")) is None
    marker_key = state_key("worker-codex", "last_clear_was_done")
    marker_present = r.get(marker_key) == "1"
    marker_ttl = r.expiry.get(marker_key)
    ok = cleared and marker_present and marker_ttl == 30
    return ok, f"current_task_cleared={cleared} marker_present={marker_present} marker_ttl={marker_ttl}"


def test_blocked_on_regression() -> tuple[bool, str]:
    r = FakeRedis()
    r.set(state_key("worker-codex", "current_task"), json.dumps({"task_id": "task-peer", "description": "Waiting on peer"}))
    r.set(state_key("worker-codex", "last_outcome"), json.dumps({"outcome": "unknown", "details": ""}))
    with mock.patch.object(shared, "_resolve_supervisor", return_value="conductor"), \
         mock.patch.object(shared, "_resolve_blocked_on", return_value="peer-response:foo"):
        shared._notify_supervisor_of_stop(r, "worker-codex", "conductor")
    ok = r.llen("taey:conductor:inbox") == 0
    return ok, f"inbox_len={r.llen('taey:conductor:inbox')}"


def test_cf_stage_b_disabled_falls_through_to_legacy() -> tuple[bool, str]:
    r = FakeRedis()
    r.set(state_key("worker-codex", "current_task"), json.dumps({"task_id": "task-legacy", "description": "legacy path"}))
    r.set(state_key("worker-codex", "last_outcome"), json.dumps({"outcome": "unknown", "details": ""}))
    # Mock _stage_b_enabled directly so test isolation is independent of both env var
    # AND production file flag at /path/to/repo (which exists post-DEPLOY).
    with mock.patch.object(shared, "_stage_b_enabled", return_value=False), \
         mock.patch.object(shared, "_resolve_supervisor", return_value="conductor"), \
         mock.patch.object(shared, "_evaluate_stop_discipline", side_effect=AssertionError("engine should not run")), \
         mock.patch.object(shared, "_resolve_blocked_on", return_value=None):
        shared._notify_supervisor_of_stop(r, "worker-codex", "conductor")
    msg = r.decoded_list("taey:conductor:inbox")[0]
    ok = msg.get("type") == "peer_idle"
    return ok, f"type={msg.get('type')}"


def test_cf_stage_b_enabled_invokes_engine() -> tuple[bool, str]:
    r = FakeRedis()
    r.set(state_key("worker-codex", "current_task"), json.dumps({"task_id": "task-engine", "description": "engine path"}))
    r.set(state_key("worker-codex", "last_outcome"), json.dumps({"outcome": "unknown", "details": ""}))
    # Mock _stage_b_enabled directly for isolation from env var + file flag state.
    with mock.patch.object(shared, "_stage_b_enabled", return_value=True), \
         mock.patch.object(shared, "_resolve_supervisor", return_value="conductor"), \
         mock.patch.object(shared, "_evaluate_stop_discipline", return_value=shared.StopDecision(wake_type=shared.WAKE_REASON_REQUIRED, body="needs reason")) as engine:
        shared._notify_supervisor_of_stop(r, "worker-codex", "conductor")
        called = engine.called
    msg = r.decoded_list("taey:conductor:inbox")[0]
    ok = called and msg.get("wake_type") == shared.WAKE_REASON_REQUIRED
    return ok, f"engine_called={called} wake_type={msg.get('wake_type')}"


def test_cf_stage_b_zero_treated_as_disabled() -> tuple[bool, str]:
    # The "0" treatment is internal logic of _stage_b_enabled(). To test it
    # specifically (rather than just the gate), call the function directly with
    # env var manipulated AND file flag mocked absent. This isolates the env-var
    # branch entirely from production file flag state.
    r = FakeRedis()
    r.set(state_key("worker-codex", "current_task"), json.dumps({"task_id": "task-zero", "description": "zero path"}))
    r.set(state_key("worker-codex", "last_outcome"), json.dumps({"outcome": "unknown", "details": ""}))
    old = os.environ.get("CF_STAGE_B_ENABLED")
    os.environ["CF_STAGE_B_ENABLED"] = "0"
    try:
        with mock.patch.object(shared.os.path, "exists", return_value=False), \
             mock.patch.object(shared, "_resolve_supervisor", return_value="conductor"), \
             mock.patch.object(shared, "_evaluate_stop_discipline", side_effect=AssertionError("engine should not run")), \
             mock.patch.object(shared, "_resolve_blocked_on", return_value=None):
            shared._notify_supervisor_of_stop(r, "worker-codex", "conductor")
    finally:
        if old is None:
            os.environ.pop("CF_STAGE_B_ENABLED", None)
        else:
            os.environ["CF_STAGE_B_ENABLED"] = old
    msg = r.decoded_list("taey:conductor:inbox")[0]
    ok = msg.get("type") == "peer_idle"
    return ok, f"type={msg.get('type')}"


def test_real_backend_wake_with_queue() -> tuple[bool, str]:
    prefix = f"stage-b-real-{uuid.uuid4().hex[:8]}"
    supervisor = f"{prefix}-supervisor"
    project_id = prefix
    phase_id = f"{prefix}-phase"
    task_id = f"{prefix}-task"
    port = _find_free_port()
    api_base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update({
        "ORCH_NEO4J_URI": os.environ["ORCH_NEO4J_URI"],
        "ORCH_REDIS_HOST": os.environ["ORCH_REDIS_HOST"],
        "ORCH_REDIS_PORT": os.environ["ORCH_REDIS_PORT"],
    })
    proc = None
    old_api_base = shared._ORCH_API_BASE
    try:
        _cleanup_real_graph(prefix)
        create_project(
            project_id=project_id,
            name="Stage B real wake project",
            supervisor=supervisor,
            priority=3,
            config=REAL_CFG,
        )
        create_phase(project_id, phase_id, "Main", config=REAL_CFG)
        create_task(
            phase_id,
            task_id,
            "Real ready task",
            priority=17,
            owner=supervisor,
            created_by="stage-b-real-test",
            wake_owner_if_ready=False,
            config=REAL_CFG,
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "lib.tasks_api:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ORCH_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_for_http(f"{api_base}/api/projects/{project_id}")
        shared._ORCH_API_BASE = api_base
        decision = shared._evaluate_stop_discipline(FakeRedis(), supervisor, task_id)
        ok = (
            decision.wake_type == shared.WAKE_WITH_QUEUE
            and decision.task_id == task_id
            and decision.project_id == project_id
            and decision.task_priority == 17
        )
        return ok, (
            f"wake_type={decision.wake_type} task_id={decision.task_id} "
            f"project_id={decision.project_id} task_priority={decision.task_priority}"
        )
    finally:
        shared._ORCH_API_BASE = old_api_base
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        _cleanup_real_graph(prefix)


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
        ("pause_allows_stop", test_pause_allows_stop),
        ("blocked_on_done_clear", test_blocked_on_done_clear),
        ("blocked_on_regression", test_blocked_on_regression),
        ("cf_stage_b_disabled_falls_through_to_legacy", test_cf_stage_b_disabled_falls_through_to_legacy),
        ("cf_stage_b_enabled_invokes_engine", test_cf_stage_b_enabled_invokes_engine),
        ("cf_stage_b_zero_treated_as_disabled", test_cf_stage_b_zero_treated_as_disabled),
        ("real_backend_wake_with_queue", test_real_backend_wake_with_queue),
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
