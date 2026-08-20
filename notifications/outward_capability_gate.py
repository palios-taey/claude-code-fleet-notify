"""Fail-closed gate for worker outward taey-notify enqueues after unbind.

P0 safety (task-7107c13f / task-f396305d): every inbox LPUSH is an outward
mutation. Caller-selected tmux/env identity (TMUX_PANE, TAEY_NODE_ID, --from)
cannot authorize. Every sender needs a live possession handle minted at bind
and revoked at unbind.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, FrozenSet, Optional

from notifications.targets import PEER_SUFFIXES, load_supervisor_topology

# Kept as documentation of the original worker-report types. The gate no longer
# skips other types for any sender.
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
    """Unknown types default gated. Control-plane types are documented only."""
    return str(msg_type or "").strip().lower() not in CONTROL_PLANE_NOTIFY_TYPES


def sender_requires_outward_capability(
    from_node: str,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> bool:
    """Historical topology helper. Authorization no longer uses this skip."""
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
    """Documented topology classification only. The gate never skips on this."""
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
    """Authorize outward notify from a possession handle. Returns orch.

    Envelope ``from_node``, ``capability_session``, tmux, and ``TAEY_NODE_ID``
    are not authorization inputs. Missing/revoked handle denies every type.
    """
    del from_node, msg_type, key_prefix, capability_session
    token = str(handle or "").strip()
    if not token:
        raise OutwardNotifyDenied("missing or revoked outward possession handle")

    try:
        from fleet_orchestrator.outward_capability import (  # type: ignore
            OutwardAuthorizationError,
            require_outward_handle,
        )
    except ImportError as exc:
        raise OutwardNotifyDenied(
            "fleet_orchestrator outward capability required; redis-only fallback removed"
        ) from exc

    try:
        require_outward_handle(token, channel="taey_notify", redis_client=redis_client)
    except OutwardAuthorizationError as exc:
        raise OutwardNotifyDenied(str(exc)) from exc
    return "orch"
