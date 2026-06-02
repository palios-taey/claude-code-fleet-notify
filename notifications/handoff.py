from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
import uuid
from typing import Any, Dict, Iterable, List


_FLAG_CACHE_TTL_S = 2.0
_FLAG_CACHE_PATH: str | None = None
_FLAG_CACHE_AT = 0.0
_FLAG_CACHE_DATA: dict[str, dict[str, bool]] = {}
_FLAG_CACHE_LOCK = threading.Lock()


def _truthy(raw: Any) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _session_items(raw: str) -> set[str]:
    return {item.strip() for item in raw.replace(";", ",").split(",") if item.strip()}


def _flag_file_map() -> dict[str, dict[str, bool]]:
    path = os.environ.get("CF_HANDOFF_SESSION_FLAGS_FILE", "").strip()
    if not path:
        return {}
    ttl_raw = os.environ.get("CF_HANDOFF_SESSION_FLAGS_TTL_SECS", str(_FLAG_CACHE_TTL_S)).strip()
    try:
        ttl_s = max(0.0, float(ttl_raw))
    except Exception:
        ttl_s = _FLAG_CACHE_TTL_S
    global _FLAG_CACHE_PATH, _FLAG_CACHE_AT, _FLAG_CACHE_DATA
    now = time.time()
    with _FLAG_CACHE_LOCK:
        if path == _FLAG_CACHE_PATH and (now - _FLAG_CACHE_AT) <= ttl_s:
            return dict(_FLAG_CACHE_DATA)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, dict[str, bool]] = {}
    for session_id, flags in payload.items():
        if not isinstance(flags, dict):
            continue
        result[str(session_id)] = {
            "enforce": _truthy(flags.get("enforce")),
            "ack_passive": _truthy(flags.get("ack_passive")),
        }
    with _FLAG_CACHE_LOCK:
        _FLAG_CACHE_PATH = path
        _FLAG_CACHE_AT = now
        _FLAG_CACHE_DATA = dict(result)
    return result


def handoff_flags_for_session(session_id: str) -> dict[str, bool]:
    file_map = _flag_file_map()
    file_flags = file_map.get(session_id, {})
    enforce_requested = _truthy(os.environ.get("CF_HANDOFF_ENFORCE"))
    ack_requested = _truthy(os.environ.get("CF_HANDOFF_ACK_PASSIVE"))
    enforce_sessions = _session_items(os.environ.get("CF_HANDOFF_ENFORCE_SESSIONS", ""))
    ack_sessions = _session_items(os.environ.get("CF_HANDOFF_ACK_PASSIVE_SESSIONS", ""))
    enforce = bool(file_flags.get("enforce")) or (enforce_requested and session_id in enforce_sessions)
    ack_passive = bool(file_flags.get("ack_passive")) or (ack_requested and session_id in ack_sessions)
    return {"enforce": enforce, "ack_passive": ack_passive}


def explicit_handoff_key(prefix: str, dispatcher: str, msg_id: str) -> str:
    return f"{prefix}:handoff:{dispatcher}:{msg_id}"


def explicit_ack_key(prefix: str, dispatcher: str, target: str, msg_id: str) -> str:
    return f"{prefix}:handoff-ack:{dispatcher}:{target}:{msg_id}"


def pending_receipts_key(prefix: str, target: str) -> str:
    return f"{prefix}:{target}:handoff_pending_receipts"


def session_machine_key(prefix: str, session_id: str) -> str:
    return f"{prefix}:{session_id}:machine"


def message_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def new_handoff_msg_id() -> str:
    return str(uuid.uuid4())


def default_pickup_poll_budget() -> int:
    raw = os.environ.get("CF_HANDOFF_PICKUP_POLL_BUDGET", "5").strip()
    try:
        return max(1, int(raw))
    except Exception:
        return 5


def is_explicit_handoff_message(message: dict[str, Any]) -> bool:
    return (
        message.get("handoff_kind") == "explicit_handoff"
        and bool(message.get("dispatcher_session_id"))
        and bool(message.get("target_session_id"))
        and bool(message.get("msg_id"))
    )


def create_explicit_handoff(
    redis_client,
    *,
    prefix: str,
    dispatcher_session_id: str,
    target_session_id: str,
    body: str,
    msg_type: str,
    priority: str,
    dispatcher_task_id: str | None = None,
    actionable_inputs: dict[str, Any] | None = None,
    ack_deadline_secs: int = 300,
) -> dict[str, Any]:
    msg_id = new_handoff_msg_id()
    msg_hash = message_hash(body)
    now = time.time()
    pickup_poll_budget = default_pickup_poll_budget()
    record = {
        "kind": "explicit_handoff",
        "dispatcher_session_id": dispatcher_session_id,
        "target_session_id": target_session_id,
        "dispatcher_task_id": dispatcher_task_id,
        "msg_id": msg_id,
        "message_hash": msg_hash,
        "actionable_inputs": actionable_inputs or {},
        "created_at": now,
        "ack_deadline_at": now + ack_deadline_secs,
        "ack_backstop_at": now + ack_deadline_secs,
        "pickup_poll_budget": pickup_poll_budget,
        "delivery_poll_count": 0,
        "delivery_state": "queued",
        "last_delivery_signal": "created",
        "last_delivery_signal_at": now,
        # Stored in local Redis state only. It identifies which daemon's local
        # tmux inventory is authoritative for tmux_missing detection.
        "origin_machine": socket.gethostname(),
    }
    payload = {
        "from": dispatcher_session_id,
        "type": msg_type,
        "body": body,
        "timestamp": now,
        "priority": priority,
        "msg_id": msg_id,
        "handoff_kind": "explicit_handoff",
        "dispatcher_session_id": dispatcher_session_id,
        "target_session_id": target_session_id,
        "dispatcher_task_id": dispatcher_task_id,
        "message_hash": msg_hash,
        "actionable_inputs": actionable_inputs or {},
        "ack_deadline_at": record["ack_deadline_at"],
        "ack_backstop_at": record["ack_backstop_at"],
        "pickup_poll_budget": pickup_poll_budget,
    }
    inbox_key = f"{prefix}:{target_session_id}:inbox"
    record_key = explicit_handoff_key(prefix, dispatcher_session_id, msg_id)
    encoded_message = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    encoded_record = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
    if hasattr(redis_client, "pipeline"):
        pipe = redis_client.pipeline(transaction=True)
        pipe.lpush(inbox_key, encoded_message)
        pipe.set(record_key, encoded_record)
        pipe.execute()
    else:
        redis_client.lpush(inbox_key, encoded_message)
        redis_client.set(record_key, encoded_record)
    return payload


def queue_pending_receipts(redis_client, *, prefix: str, target_session_id: str,
                           messages: Iterable[dict[str, Any]]) -> None:
    pending_key = pending_receipts_key(prefix, target_session_id)
    raw_existing = redis_client.get(pending_key)
    try:
        existing = json.loads(raw_existing) if raw_existing else []
    except Exception:
        existing = []
    if not isinstance(existing, list):
        existing = []
    seen = {
        (
            item.get("dispatcher_session_id"),
            item.get("target_session_id"),
            item.get("msg_id"),
        )
        for item in existing
        if isinstance(item, dict)
    }
    changed = False
    for message in messages:
        if not is_explicit_handoff_message(message):
            continue
        entry = {
            "dispatcher_session_id": str(message.get("dispatcher_session_id")),
            "target_session_id": str(message.get("target_session_id")),
            "msg_id": str(message.get("msg_id")),
            "message_hash": str(message.get("message_hash") or ""),
        }
        token = (
            entry["dispatcher_session_id"],
            entry["target_session_id"],
            entry["msg_id"],
        )
        if token in seen:
            continue
        existing.append(entry)
        seen.add(token)
        changed = True
    if changed:
        redis_client.set(pending_key, json.dumps(existing, separators=(",", ":")))


def mark_session_machine(redis_client, *, prefix: str, session_id: str, machine: str,
                         ttl_secs: int = 120) -> None:
    redis_client.set(session_machine_key(prefix, session_id), machine, ex=ttl_secs)


def _pending_handoff_messages(redis_client, *, prefix: str, target_session_id: str) -> list[dict[str, str]]:
    inbox_key = f"{prefix}:{target_session_id}:inbox"
    pending: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in redis_client.lrange(inbox_key, 0, -1):
        try:
            message = json.loads(raw)
        except Exception:
            continue
        if not is_explicit_handoff_message(message):
            continue
        dispatcher = str(message.get("dispatcher_session_id") or "")
        msg_id = str(message.get("msg_id") or "")
        if not dispatcher or not msg_id:
            continue
        token = (dispatcher, msg_id)
        if token in seen:
            continue
        seen.add(token)
        pending.append({
            "dispatcher_session_id": dispatcher,
            "target_session_id": str(message.get("target_session_id") or target_session_id),
            "msg_id": msg_id,
            "message_hash": str(message.get("message_hash") or ""),
        })
    return pending


def record_delivery_signal(
    redis_client,
    *,
    prefix: str,
    target_session_id: str,
    signal: str,
    signal_source: str,
    machine: str,
) -> int:
    now = time.time()
    updates = 0
    for pending in _pending_handoff_messages(
        redis_client,
        prefix=prefix,
        target_session_id=target_session_id,
    ):
        dispatcher = pending["dispatcher_session_id"]
        msg_id = pending["msg_id"]
        record_key = explicit_handoff_key(prefix, dispatcher, msg_id)
        try:
            record = json.loads(redis_client.get(record_key) or "")
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("kind") != "explicit_handoff":
            continue
        if str(record.get("target_session_id") or "") != target_session_id:
            continue
        if str(record.get("state") or "") in {"resolved", "superseded", "dead", "receipt_acked"}:
            continue
        record["last_delivery_signal"] = signal
        record["last_delivery_signal_at"] = now
        record["delivery_signal_source"] = signal_source
        record["delivery_machine"] = machine
        if signal == "inject_ok":
            record["delivery_state"] = "injected_waiting_ack"
            record["delivery_poll_count"] = int(record.get("delivery_poll_count", 0) or 0) + 1
            record.setdefault("first_injected_at", now)
            record["last_injected_at"] = now
        elif signal in {"inject_failed", "tmux_missing"}:
            record["delivery_state"] = "not_deliverable"
            record["delivery_failure_reason"] = signal
            record.setdefault("first_delivery_failure_at", now)
            record["last_delivery_failure_at"] = now
        else:
            continue
        redis_client.set(record_key, json.dumps(record, separators=(",", ":")))
        updates += 1
    return updates


def flush_pending_receipts(redis_client, *, prefix: str, target_session_id: str,
                           ack_passive_enabled: bool) -> list[dict[str, Any]]:
    pending_key = pending_receipts_key(prefix, target_session_id)
    raw_existing = redis_client.get(pending_key)
    try:
        existing = json.loads(raw_existing) if raw_existing else []
    except Exception:
        existing = []
    if not isinstance(existing, list) or not existing:
        return []
    if not ack_passive_enabled:
        return []
    written: list[dict[str, Any]] = []
    for item in existing:
        if not isinstance(item, dict):
            continue
        dispatcher = str(item.get("dispatcher_session_id") or "")
        target = str(item.get("target_session_id") or "")
        msg_id = str(item.get("msg_id") or "")
        if not dispatcher or not target or not msg_id:
            continue
        ack_payload = {
            "ack_by": target_session_id,
            "message_hash": str(item.get("message_hash") or ""),
        }
        redis_client.set(
            explicit_ack_key(prefix, dispatcher, target, msg_id),
            json.dumps(ack_payload, separators=(",", ":")),
        )
        written.append(ack_payload | {"dispatcher_session_id": dispatcher, "msg_id": msg_id})
    redis_client.delete(pending_key)
    return written
