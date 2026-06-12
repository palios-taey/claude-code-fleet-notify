from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any


_ORCH_API_BASE = os.environ.get("ORCH_API_BASE", "http://127.0.0.1:5002")


def _api_json(path: str, *, timeout: int = 3) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{_ORCH_API_BASE}{path}",
        method="GET",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)
    return payload if isinstance(payload, dict) else {}


def resolve_in_progress_task(task_id: Any) -> tuple[bool, str, dict[str, Any] | None]:
    task_id_text = str(task_id or "").strip()
    if not task_id_text:
        return False, "missing_task_id", None
    try:
        task = _api_json(f"/api/tasks/{urllib.parse.quote(task_id_text, safe='')}")
    except Exception:
        return False, "task_unresolved", None
    status = str(task.get("status") or "").strip().lower()
    if status != "in_progress":
        return False, f"task_status_{status or 'unknown'}", task
    return True, "in_progress", task


def peer_idle_allowed(task_id: Any, node_id: str, supervisor: Any) -> tuple[bool, str, dict[str, Any] | None]:
    supervisor_text = str(supervisor or "").strip()
    if not supervisor_text:
        return False, "missing_supervisor", None
    if supervisor_text == node_id:
        return False, "self_supervisor", None
    return resolve_in_progress_task(task_id)
