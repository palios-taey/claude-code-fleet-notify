#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORCH_ROOT = Path(os.environ.get("ORCH_ROOT", str(ROOT.parent / "claude-code-fleet-orchestrator")))
sys.path.insert(0, str(ORCH_ROOT))

PREFIX = f"stophook-{uuid.uuid4().hex[:8]}"
if "ORCH_DOTENV" not in os.environ:
    candidate = ORCH_ROOT / ".env"
    if candidate.is_file():
        os.environ["ORCH_DOTENV"] = str(candidate)
os.environ["NOTIFY_KEY_PREFIX"] = PREFIX
os.environ.setdefault("ORCH_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("ORCH_REDIS_PORT", "6379")

from lib.config import OrchConfig, get_neo4j_driver, get_redis_sync  # noqa: E402
from lib.orch_schema import create_phase, create_project, create_task, update_task_status  # noqa: E402

CFG = OrchConfig()


def _ensure_dotenv_for_server() -> str:
    explicit = os.environ.get("ORCH_DOTENV")
    if explicit:
        return explicit
    handle = tempfile.NamedTemporaryFile("w", delete=False, suffix=".env")
    for key in (
        "ORCH_REDIS_HOST",
        "ORCH_REDIS_PORT",
        "ORCH_NEO4J_URI",
        "ORCH_NEO4J_USER",
        "ORCH_NEO4J_PASS",
        "ORCH_NEO4J_DB",
        "ORCH_DASHBOARD_URL",
        "ORCH_NOTIFY_LIB_ROOT",
        "ORCH_NOTIFY_CLI",
    ):
        value = os.environ.get(key)
        if value:
            handle.write(f"{key}={value}\n")
    handle.flush()
    handle.close()
    return handle.name


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
        except Exception as exc:
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"server did not become ready: {last_error}")


def _cleanup(prefix: str) -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (t:OrchTask) WHERE t.id STARTS WITH $prefix DETACH DELETE t", prefix=prefix)
        session.run("MATCH (ph:OrchPhase) WHERE ph.id STARTS WITH $prefix DETACH DELETE ph", prefix=prefix)
        session.run("MATCH (p:OrchProject) WHERE p.id STARTS WITH $prefix DETACH DELETE p", prefix=prefix)
    r = get_redis_sync(CFG)
    cursor = 0
    pattern = f"{PREFIX}:*"
    while True:
        cursor, keys = r.scan(cursor=cursor, match=pattern, count=100)
        if keys:
            r.delete(*keys)
        if cursor == 0:
            break


def _wire_dependency(task_id: str, dep_id: str) -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run(
            """
            MATCH (t:OrchTask {id: $task_id}), (dep:OrchTask {id: $dep_id})
            MERGE (t)-[:DEPENDS_ON]->(dep)
            """,
            task_id=task_id,
            dep_id=dep_id,
        )


def _run_hook(path: Path, api_base: str) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["TAEY_NODE_ID"] = "conductor-codex"
    env["NOTIFY_KEY_PREFIX"] = PREFIX
    env["ORCH_API_BASE"] = api_base
    proc = subprocess.run(
        [sys.executable, str(path)],
        input='{"stop_hook_active": true}',
        text=True,
        capture_output=True,
        cwd=str(ROOT),
        env=env,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def main() -> int:
    project_id = f"{PREFIX}-project"
    phase_id = f"{PREFIX}-phase"
    task1 = f"{PREFIX}-task-1"
    task2 = f"{PREFIX}-task-2"
    port = _find_free_port()
    server_env = os.environ.copy()
    server_env["NOTIFY_KEY_PREFIX"] = PREFIX
    server_env["ORCH_DOTENV"] = _ensure_dotenv_for_server()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "lib.tasks_api:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ORCH_ROOT),
        env=server_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    api_base = f"http://127.0.0.1:{port}"
    try:
        _cleanup(PREFIX)
        create_project(project_id, "Stop Hook API Boundary", supervisor="conductor", config=CFG)
        create_phase(project_id, phase_id, "Main", config=CFG)
        create_task(phase_id, task1, "First ready task", owner="conductor-codex", wake_owner_if_ready=False, config=CFG)
        create_task(phase_id, task2, "Second ready task", owner="conductor-codex", wake_owner_if_ready=False, config=CFG)
        _wire_dependency(task2, task1)

        _wait_for_http(f"{api_base}/health")

        live = []
        rc, out, err = _run_hook(ROOT / "hooks" / "codex_stop.py", api_base)
        live.append(("step1", rc, out, err))
        update_task_status(task1, "completed", owner="conductor-codex", config=CFG)
        rc, out, err = _run_hook(ROOT / "hooks" / "codex_stop.py", api_base)
        live.append(("step2", rc, out, err))
        update_task_status(task2, "completed", owner="conductor-codex", config=CFG)
        rc, out, err = _run_hook(ROOT / "hooks" / "codex_stop.py", api_base)
        live.append(("step3", rc, out, err))

        first_ok = live[0][1] == 0 and '"decision": "block"' in live[0][2] and task1 in live[0][2]
        second_ok = live[1][1] == 0 and '"decision": "block"' in live[1][2] and task2 in live[1][2]
        third_ok = live[2][1] == 0 and live[2][2] == "{}" and live[2][3] == ""
        transcript = " | ".join(f"{name}:rc={rc} stdout={out} stderr={err}" for name, rc, out, err in live)
        print("PASS live_cycle " + transcript if first_ok and second_ok and third_ok else "FAIL live_cycle " + transcript)

        dead_api = f"http://127.0.0.1:{_find_free_port()}"
        for hook_name in ("stop_idle.py", "codex_stop.py", "gemini_after_agent.py"):
            rc, out, err = _run_hook(ROOT / "hooks" / hook_name, dead_api)
            ok = rc == 0 and out == "{}" and err == ""
            print(
                f"{'PASS' if ok else 'FAIL'} api_down_{hook_name} rc={rc} stdout={out} stderr={err}"
            )

        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        _cleanup(PREFIX)


if __name__ == "__main__":
    raise SystemExit(main())
