"""Redis-backed inter-session inbox utilities.

Queue layout
------------
- ``${NOTIFY_KEY_PREFIX:-taey}:{node_id}:inbox`` — inter-session messages (writers ``LPUSH``, readers ``RPOP``)
- ``${NOTIFY_KEY_PREFIX:-taey}:{node_id}:notifications`` — monitor / worker notifications (writers ``RPUSH``, readers ``LPOP``)
- ``${NOTIFY_KEY_PREFIX:-taey}:notify:{node_id}:orch`` — auxiliary notifications (treated as ``RPUSH``/``LPOP``)

Delivery paths
--------------
1. Active sessions: PostToolUse hook drains queues inline and renders the payload via
   ``hookSpecificOutput.additionalContext``.
2. Stopped sessions: notification daemon injects a Redis pointer via ``scripts/tmux-send``
   when the explicit idle flag is set, or when recent activity is stale beyond the
   maximum tool window and no tool is running. Queues are drained only by recipient
   hooks after a real prompt/tool event.

The helpers in this module intentionally avoid ``DELETE``-ing whole queues during normal
operation because that can drop messages that arrive between a destructive peek and the
clear step.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, Iterable, Optional

MAX_TOOL_RUNTIME_SEC = 600
DEFAULT_TOOL_TTL = 900
DEFAULT_INJECT_IDLE_GRACE_SEC = 900.0
DEFAULT_KEY_PREFIX = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
WAKE_ALLOW_STOP = "ALLOW_STOP"
WAKE_WITH_QUEUE = "WAKE_WITH_QUEUE"
WAKE_REASON_REQUIRED = "WAKE_REASON_REQUIRED"
WAKE_ENGINE_ERROR = "ENGINE_ERROR"
WAKE_TYPES = {WAKE_ALLOW_STOP, WAKE_WITH_QUEUE, WAKE_REASON_REQUIRED, WAKE_ENGINE_ERROR}
# No TTL on idle. Stopped means stopped until UserPromptSubmit clears it.
# State doesn't decay just because time passed — a session at rest stays at rest.


def key_prefix() -> str:
    """Return the Redis key prefix selected at module import time."""
    return DEFAULT_KEY_PREFIX


def set_key_prefix(prefix: str) -> None:
    """Override the process-local Redis key prefix for CLI argument handling."""
    global DEFAULT_KEY_PREFIX
    DEFAULT_KEY_PREFIX = prefix


def inbox_key(node_id: str) -> str:
    return f"{DEFAULT_KEY_PREFIX}:{node_id}:inbox"


def notifications_key(node_id: str) -> str:
    return f"{DEFAULT_KEY_PREFIX}:{node_id}:notifications"


def orch_key(node_id: str) -> str:
    return f"{DEFAULT_KEY_PREFIX}:notify:{node_id}:orch"


def state_key(node_id: str, suffix: str) -> str:
    return f"{DEFAULT_KEY_PREFIX}:{node_id}:{suffix}"


def new_msg_id() -> str:
    """Return a short message id suitable for dedup / operator logs."""
    return uuid.uuid4().hex[:12]


def build_message(
    body: str,
    msg_type: str = "message",
    from_node: str = "unknown",
    priority: str = "normal",
    **extra: Any,
) -> Dict[str, Any]:
    """Build a normalized message payload."""
    payload: Dict[str, Any] = {
        "from": from_node,
        "type": msg_type,
        "body": body,
        "timestamp": time.time(),
        "priority": priority,
        "msg_id": extra.pop("msg_id", new_msg_id()),
    }
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return payload


def _decode_message(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {"type": "empty", "raw": ""}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"raw": str(raw), "type": "unparseable", "msg_id": new_msg_id()}


def _encode_message(message: Dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False)


def _pop_many(redis_client, key: str, pop_method: str, max_count: Optional[int] = None) -> list[dict]:
    """Pop up to ``max_count`` items using the named Redis pop method."""
    messages: list[dict] = []
    remaining = max_count
    while remaining is None or remaining > 0:
        raw = getattr(redis_client, pop_method)(key)
        if not raw:
            break
        messages.append(_decode_message(raw))
        if remaining is not None:
            remaining -= 1
    return messages


def _peek_lpush_queue(redis_client, key: str, max_count: int) -> list[dict]:
    """Peek a queue written with LPUSH and read with RPOP (oldest at tail)."""
    raw_items = list(redis_client.lrange(key, -max_count, -1))
    raw_items.reverse()
    return [_decode_message(raw) for raw in raw_items]


def _peek_rpush_queue(redis_client, key: str, max_count: int) -> list[dict]:
    """Peek a queue written with RPUSH and read with LPOP (oldest at head)."""
    raw_items = redis_client.lrange(key, 0, max_count - 1)
    return [_decode_message(raw) for raw in raw_items]


def send(
    redis_client,
    target_node: str,
    body: str,
    msg_type: str = "message",
    from_node: str = "unknown",
    priority: str = "normal",
    **extra: Any,
) -> bool:
    """Send a message to a node's inbox."""
    payload = build_message(
        body=body,
        msg_type=msg_type,
        from_node=from_node,
        priority=priority,
        **extra,
    )
    redis_client.lpush(inbox_key(target_node), _encode_message(payload))
    try:
        from notifications.trace import trace
        trace(redis_client, "enqueue", node=target_node, src="inbox", type=msg_type, frm=from_node)
    except Exception:
        pass
    return True


def send_notification(
    redis_client,
    target_node: str,
    body: str,
    msg_type: str = "notification",
    from_node: str = "unknown",
    priority: str = "normal",
    **extra: Any,
) -> bool:
    """Send a message to a node's notification queue."""
    payload = build_message(
        body=body,
        msg_type=msg_type,
        from_node=from_node,
        priority=priority,
        **extra,
    )
    redis_client.rpush(notifications_key(target_node), _encode_message(payload))
    try:
        from notifications.trace import trace
        trace(redis_client, "enqueue", node=target_node, src="notifications", type=msg_type, frm=from_node)
    except Exception:
        pass
    return True


def receive(redis_client, node_id: str, max_count: int = 10) -> list[dict]:
    """Pop messages from the inter-session inbox (FIFO)."""
    return _pop_many(redis_client, inbox_key(node_id), "rpop", max_count)


def receive_notifications(redis_client, node_id: str, max_count: int = 10) -> list[dict]:
    """Pop monitor / worker notifications (FIFO)."""
    return _pop_many(redis_client, notifications_key(node_id), "lpop", max_count)


def receive_orch(redis_client, node_id: str, max_count: int = 10) -> list[dict]:
    """Pop orchestration notifications (FIFO)."""
    return _pop_many(redis_client, orch_key(node_id), "lpop", max_count)


def peek_count(redis_client, node_id: str) -> int:
    return int(redis_client.llen(inbox_key(node_id)) or 0)


def peek_notifications_count(redis_client, node_id: str) -> int:
    return int(redis_client.llen(notifications_key(node_id)) or 0)


def peek_orch_count(redis_client, node_id: str) -> int:
    return int(redis_client.llen(orch_key(node_id)) or 0)


def has_pending_messages(redis_client, node_id: str) -> bool:
    return (
        peek_count(redis_client, node_id) > 0
        or peek_notifications_count(redis_client, node_id) > 0
        or peek_orch_count(redis_client, node_id) > 0
    )


def peek_all(redis_client, node_id: str, max_count: int = 20) -> dict:
    """Peek at all message sources without consuming them."""
    return {
        "inbox": _peek_lpush_queue(redis_client, inbox_key(node_id), max_count),
        "notifications": _peek_rpush_queue(redis_client, notifications_key(node_id), max_count),
        "orch": _peek_rpush_queue(redis_client, orch_key(node_id), max_count),
    }


def drain_all(redis_client, node_id: str, max_count: Optional[int] = None) -> dict:
    """Consume all queued messages for a node.

    Returned lists are ordered oldest → newest so callers can render them directly.
    """
    result = {
        "inbox": _pop_many(redis_client, inbox_key(node_id), "rpop", max_count),
        "notifications": _pop_many(redis_client, notifications_key(node_id), "lpop", max_count),
        "orch": _pop_many(redis_client, orch_key(node_id), "lpop", max_count),
    }
    try:
        n = len(result["inbox"]) + len(result["notifications"]) + len(result["orch"])
        if n:
            from notifications.trace import trace
            trace(redis_client, "drain", node=node_id, count=n)
    except Exception:
        pass
    return result


def requeue_all(redis_client, node_id: str, messages_by_source: dict) -> None:
    """Restore a drained batch back into Redis preserving FIFO order."""
    for msg in messages_by_source.get("inbox", []):
        redis_client.lpush(inbox_key(node_id), _encode_message(msg))
    for msg in messages_by_source.get("notifications", []):
        redis_client.rpush(notifications_key(node_id), _encode_message(msg))
    for msg in messages_by_source.get("orch", []):
        redis_client.rpush(orch_key(node_id), _encode_message(msg))


def clear_all(redis_client, node_id: str, max_count: Optional[int] = None) -> None:
    """Compatibility wrapper: drain and discard queued messages.

    This is intentionally implemented as pops, not ``DELETE``, to avoid dropping messages
    that arrive between a separate peek and clear operation.
    """
    drained = drain_all(redis_client, node_id, max_count=max_count)
    from notifications.handoff import queue_pending_receipts
    queue_pending_receipts(
        redis_client,
        prefix=DEFAULT_KEY_PREFIX,
        target_session_id=node_id,
        messages=flatten_sources(drained),
    )


def flatten_sources(messages_by_source: dict) -> list[dict]:
    """Flatten the three queue sources into the display order used by hooks/daemon."""
    return (
        list(messages_by_source.get("inbox", []))
        + list(messages_by_source.get("notifications", []))
        + list(messages_by_source.get("orch", []))
    )


def format_message(msg: Dict[str, Any]) -> str:
    """Format a single message for terminal / additionalContext display."""
    sender = msg.get("from", msg.get("platform", "unknown"))
    priority = str(msg.get("priority", "")).lower()
    # NEVER use '!!!' as a priority marker — bash history expansion breaks it
    # in shell-piped contexts (per NOTIFICATION_PROTOCOL.md). Use 'URGENT '.
    prefix = "URGENT " if priority == "high" else ""

    if msg.get("status") == "response_complete":
        platform = msg.get("platform", "unknown")
        elapsed = msg.get("elapsed_seconds")
        elapsed_text = f" ({elapsed}s)" if elapsed not in (None, "") else ""
        return (
            f"  *** {platform.upper()} RESPONSE READY{elapsed_text} ***\n"
            f"  ACTION: taey_quick_extract(platform='{platform}', complete=True)"
        )

    wake_type = msg.get("wake_type")
    if wake_type in WAKE_TYPES:
        lines = [f"  *** {wake_type} from {sender} ***"]
        if msg.get("project_id"):
            lines.append(f"  project: {msg['project_id']}")
        if msg.get("task_id"):
            lines.append(f"  task: {msg['task_id']}")
        if msg.get("task_title_short"):
            lines.append(f"  title: {msg['task_title_short']}")
        if msg.get("priority") is not None:
            lines.append(f"  priority: {msg['priority']}")
        if msg.get("task_priority") is not None:
            lines.append(f"  task_priority: {msg['task_priority']}")
        if msg.get("available_conditions"):
            labels = ", ".join(
                f"{cond.get('label')} [{cond.get('condition_id')} v{cond.get('version')}]"
                for cond in msg.get("available_conditions", [])
            )
            lines.append(f"  available_conditions: {labels}")
        if msg.get("resume_context_pointer"):
            lines.append(f"  resume_context_pointer: {msg['resume_context_pointer']}")
        if msg.get("next_action"):
            lines.append(f"  ACTION: {msg['next_action']}")
        body = msg.get("body")
        if body:
            lines.append(f"  detail: {body}")
        return "\n".join(lines)

    mtype = str(msg.get("type", msg.get("status", "message"))).upper()
    body = msg.get("body", msg.get("message", msg.get("raw", str(msg))))
    return f"  {prefix}[{mtype} from {sender}]: {body}"


def format_notification_block(
    messages: Iterable[Dict[str, Any]],
    task_summary: str = "",
    header: str = "=== NOTIFICATIONS ===",
) -> str:
    """Render a full notification block for hooks / tmux injection."""
    materialized = list(messages)
    lines = ["", header]
    for msg in materialized:
        lines.append(format_message(msg))
    if task_summary:
        lines.append(task_summary)
    lines.append("=====================")
    lines.append("")
    return "\n".join(lines)


def mark_activity(redis_client, node_id: str) -> None:
    redis_client.set(state_key(node_id, "last_activity"), str(time.time()))


def set_tool_running(redis_client, node_id: str, ttl: int = DEFAULT_TOOL_TTL) -> None:
    """Mark a node as mid-tool-call. TTL outlives max tool runtime and is only a crash safety net."""
    redis_client.set(state_key(node_id, "tool_running"), "1", ex=ttl)
    mark_activity(redis_client, node_id)


def clear_tool_running(redis_client, node_id: str) -> None:
    redis_client.delete(state_key(node_id, "tool_running"))
    mark_activity(redis_client, node_id)


def set_idle(redis_client, node_id: str) -> None:
    redis_client.set(state_key(node_id, "idle"), "1")
    mark_activity(redis_client, node_id)


def clear_idle(redis_client, node_id: str) -> None:
    redis_client.delete(state_key(node_id, "idle"))
    mark_activity(redis_client, node_id)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def inject_idle_grace_sec() -> float:
    raw = os.environ.get("INJECT_IDLE_GRACE_SEC", str(DEFAULT_INJECT_IDLE_GRACE_SEC))
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_INJECT_IDLE_GRACE_SEC


def is_node_idle(redis_client, node_id: str, idle_threshold: int = 30) -> bool:  # noqa: ARG001
    """Check whether a node is safe for tmux injection.

    ONLY returns True if the explicit idle flag is set by the Stop hook.
    """
    return bool(redis_client.exists(state_key(node_id, "idle")))


def can_inject_pointer(
    redis_client,
    node_id: str,
    *,
    now: float | None = None,
    idle_grace_sec: float | None = None,
) -> bool:
    """Return True when daemon pointer injection is safe for this session.

    The explicit idle flag remains the fast path. If that best-effort flag is
    absent, the daemon may still inject only after the session has no active
    tool-running marker and its last activity is older than a grace window that
    exceeds the maximum tool runtime.
    """
    return pointer_injection_path(
        redis_client,
        node_id,
        now=now,
        idle_grace_sec=idle_grace_sec,
    ) is not None


def pointer_injection_path(
    redis_client,
    node_id: str,
    *,
    now: float | None = None,
    idle_grace_sec: float | None = None,
) -> str | None:
    """Return the signal that authorizes pointer injection, or None."""
    if redis_client.exists(state_key(node_id, "tool_running")):
        return None
    if is_node_idle(redis_client, node_id):
        return "idle"
    last_activity = _float_or_none(redis_client.get(state_key(node_id, "last_activity")))
    if last_activity is None:
        return None
    now = time.time() if now is None else now
    grace = inject_idle_grace_sec() if idle_grace_sec is None else max(0.0, float(idle_grace_sec))
    if (now - last_activity) > grace:
        return "stale"
    return None
