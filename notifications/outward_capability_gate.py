"""Fail-closed gate for worker outward taey-notify enqueues after unbind.

P0 safety (task-7107c13f / task-f396305d): every inbox LPUSH is an outward
mutation. Authorization identity is the process TTY → tmux session, never
TMUX_PANE, TAEY_NODE_ID, or --from. Workers need a live current_task.
Supervisors retain a distinct control-plane send path when that TTY identity
is a topology supervisor.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, FrozenSet, Optional

from notifications.targets import PEER_SUFFIXES, load_supervisor_topology

WORKER_OUTWARD_NOTIFY_TYPES: FrozenSet[str] = frozenset(
    {"response_ready", "result", "defect", "status"}
)
CONTROL_PLANE_NOTIFY_TYPES: FrozenSet[str] = frozenset(
    {
        "command",
        "wake",
        "directive",
        "heartbeat",
        "message",
        "notification",
        "escalation",
        "task",
    }
)


class OutwardNotifyDenied(RuntimeError):
    """Worker outward notify denied after unbind/revocation."""


def notify_type_requires_outward_capability(msg_type: str) -> bool:
    """Unknown types default gated. Only control-plane types may skip."""
    return str(msg_type or "").strip().lower() not in CONTROL_PLANE_NOTIFY_TYPES


def sender_requires_outward_capability(
    from_node: str,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> bool:
    """Workers need a live binding; topology supervisors are control-plane."""
    session = str(from_node or "").strip()
    if not session:
        return True
    topology = load_supervisor_topology(environ)
    if topology is not None:
        return session not in topology.supervisors
    return any(session.endswith(suffix) for suffix in PEER_SUFFIXES)


def is_control_plane_exception(
    from_node: str,
    msg_type: str,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> bool:
    """TTY-identity supervisors may skip control-plane types. Unknown types never skip."""
    if notify_type_requires_outward_capability(msg_type):
        return False
    return not sender_requires_outward_capability(from_node, environ=environ)


def require_outward_notify_capability(
    from_node: str,
    msg_type: str,
    redis_client: Any,
    *,
    key_prefix: str = "taey",
    capability_session: Optional[str] = None,
    handle: Optional[str] = None,
) -> str:
    """Authorize outward notify from TTY-backed process identity.

    Envelope ``from_node``/``--from``/``TAEY_NODE_ID``/``handle`` cannot skip
    the gate. ``capability_session`` must be the TTY-mapped tmux session.
    """
    del handle
    if capability_session is None:
        identity = str(from_node or "").strip()
    else:
        identity = str(capability_session).strip()
    if not identity:
        raise OutwardNotifyDenied("process tty/session identity is required for outward notify")
    if is_control_plane_exception(identity, msg_type):
        return "skip"

    try:
        from fleet_orchestrator.outward_capability import (  # type: ignore
            OutwardAuthorizationError,
            require_outward_capability,
        )
    except ImportError as exc:
        raise OutwardNotifyDenied(
            "fleet_orchestrator outward capability required; redis-only fallback removed"
        ) from exc

    try:
        require_outward_capability(identity, channel="taey_notify", redis_client=redis_client)
    except OutwardAuthorizationError as exc:
        raise OutwardNotifyDenied(str(exc)) from exc
    return "orch"
