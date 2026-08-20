"""Fail-closed gate for worker outward taey-notify enqueues after unbind.

P0 safety (task-f396305d): when a worker sends response_ready/result/defect/status,
unbind/revocation must deny the Redis inbox mutation even if the process is still
running.

Prefers the orchestrator's shared ``authorize_outward_capability`` when
``fleet_orchestrator`` is importable (one shared boundary with GitHub status/comment).
Falls back to Redis ``current_task`` presence — the same key ``taey-task unbind``
clears — when orch is not installed.
"""
from __future__ import annotations

import json
from typing import Any, FrozenSet, Optional

WORKER_OUTWARD_NOTIFY_TYPES: FrozenSet[str] = frozenset(
    {"response_ready", "result", "defect", "status"}
)


class OutwardNotifyDenied(RuntimeError):
    """Worker outward notify denied after unbind/revocation."""


def notify_type_requires_outward_capability(msg_type: str) -> bool:
    return str(msg_type or "").strip().lower() in WORKER_OUTWARD_NOTIFY_TYPES


def _decode_current_task(raw: Any) -> Optional[dict]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _redis_current_task_gate(redis_client: Any, from_node: str, *, key_prefix: str) -> None:
    key = f"{key_prefix}:{from_node}:current_task"
    try:
        raw = redis_client.get(key)
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise OutwardNotifyDenied(
            f"redis current_task read failed (fail-closed): {exc}"
        ) from exc
    current = _decode_current_task(raw)
    if not current:
        raise OutwardNotifyDenied(
            f"no live current_task binding for session {from_node}; "
            "mutation denied after unbind/revocation"
        )
    if not str(current.get("task_id") or "").strip():
        raise OutwardNotifyDenied(
            f"current_task for {from_node} lacks task_id; mutation denied"
        )
    if not str(current.get("supervisor") or "").strip():
        raise OutwardNotifyDenied(
            f"current_task for {from_node} lacks supervisor; mutation denied"
        )


def require_outward_notify_capability(
    from_node: str,
    msg_type: str,
    redis_client: Any,
    *,
    key_prefix: str = "taey",
) -> str:
    """Authorize worker outward notify. Returns which gate fired: orch|redis|skip."""
    if not notify_type_requires_outward_capability(msg_type):
        return "skip"

    session = str(from_node or "").strip()
    if not session:
        raise OutwardNotifyDenied("from_node is required for outward notify mutation")

    # Shared boundary with GitHub when orchestrator is on PYTHONPATH.
    try:
        from fleet_orchestrator.outward_capability import (  # type: ignore
            OutwardAuthorizationError,
            require_outward_capability,
        )
    except ImportError:
        _redis_current_task_gate(redis_client, session, key_prefix=key_prefix)
        return "redis"

    try:
        require_outward_capability(session, channel="taey_notify", redis_client=redis_client)
    except OutwardAuthorizationError as exc:
        raise OutwardNotifyDenied(str(exc)) from exc
    return "orch"
