"""Notify target resolution helpers."""
from __future__ import annotations

import os
from typing import Optional


PEER_SUFFIXES = ("-codex", "-gemini", "-grok")


def _decode(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace").strip()
    return str(raw).strip()


def _state_key(prefix: str, node_id: str, suffix: str) -> str:
    return f"{prefix}:{node_id}:{suffix}"


def _explicit_parent(redis_client, node_id: str, prefix: str) -> str:
    try:
        return _decode(redis_client.get(_state_key(prefix, node_id, "parent")))
    except Exception:
        return ""


def resolve_supervisor(redis_client, node_id: str, prefix: Optional[str] = None) -> Optional[str]:
    """Resolve the supervisor for ``node_id`` using the fleet parent rule."""
    if not node_id:
        return None
    key_prefix = prefix or os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    for suffix in PEER_SUFFIXES:
        if node_id.endswith(suffix):
            suffix_supervisor = node_id[: -len(suffix)]
            explicit = _explicit_parent(redis_client, node_id, key_prefix)
            if explicit and explicit != node_id:
                return explicit
            return suffix_supervisor

    explicit = _explicit_parent(redis_client, node_id, key_prefix)
    return explicit or None


def default_notify_target(
    redis_client,
    from_node: str,
    explicit_target: Optional[str] = None,
    prefix: Optional[str] = None,
) -> Optional[str]:
    """Return the default target for peer-authored defect/status/result reports."""
    target = (explicit_target or "").strip()
    if target:
        return target
    supervisor = resolve_supervisor(redis_client, from_node, prefix=prefix)
    if supervisor and supervisor != from_node:
        return supervisor
    return None
