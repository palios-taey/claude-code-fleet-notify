"""Notify target resolution helpers."""
from __future__ import annotations

import os
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Optional


PEER_SUFFIXES = ("-codex", "-gemini", "-grok")
READER_DRAIN_MARKER_SUFFIXES = ("last_drain_at", "last_read_at")


def _decode(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace").strip()
    return str(raw).strip()


def _state_key(prefix: str, node_id: str, suffix: str) -> str:
    return f"{prefix}:{node_id}:{suffix}"


def _split_targets(raw: str) -> set[str]:
    return {item.strip() for item in raw.replace(";", ",").split(",") if item.strip()}


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


def get_local_tmux_sessions() -> set[str]:
    """Return local tmux session names, or an empty set when tmux is unavailable."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return set()
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def registered_session_targets(api_base: Optional[str] = None, timeout: Optional[float] = None) -> set[str]:
    """Return explicitly registered session names from env and the orchestrator API."""
    registered = _split_targets(os.environ.get("CF_NOTIFY_REGISTERED_TARGETS", ""))
    base = (api_base or os.environ.get("ORCH_API_BASE", "http://127.0.0.1:5002")).strip()
    if not base:
        return registered
    if timeout is None:
        try:
            timeout = max(0.1, float(os.environ.get("CF_NOTIFY_SESSION_API_TIMEOUT", "1.0")))
        except ValueError:
            timeout = 1.0
    url = urllib.parse.urljoin(base.rstrip("/") + "/", "api/sessions")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except Exception:
        return registered
    try:
        import json

        data = json.loads(payload)
    except Exception:
        return registered
    sessions = data.get("sessions") if isinstance(data, dict) else data
    if isinstance(sessions, list):
        registered.update(str(item).strip() for item in sessions if str(item).strip())
    return registered


def _redis_scan(redis_client, pattern: str) -> list[str]:
    try:
        return [_decode(key) for key in redis_client.scan_iter(match=pattern, count=1000)]
    except TypeError:
        return [_decode(key) for key in redis_client.scan_iter(match=pattern)]
    except Exception:
        return []


def _node_from_state_key(prefix: str, key: str, suffix: str) -> str:
    head = f"{prefix}:"
    tail = f":{suffix}"
    if key.startswith(head) and key.endswith(tail):
        return key[len(head): -len(tail)]
    return ""


def _float_or_none(raw) -> Optional[float]:
    try:
        return float(_decode(raw))
    except (TypeError, ValueError):
        return None


def _reader_liveness_window() -> float:
    raw = os.environ.get("CF_NOTIFY_LAST_ACTIVITY_MAX_AGE_SECS")
    if raw is None:
        raw = os.environ.get("CF_NOTIFY_READER_LIVENESS_SECS", "300")
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 300.0


def _scan_recent_timestamp_targets(redis_client, prefix: str, suffix: str, now: float, max_age: float) -> set[str]:
    targets: set[str] = set()
    for key in _redis_scan(redis_client, f"{prefix}:*:{suffix}"):
        timestamp = None
        try:
            timestamp = _float_or_none(redis_client.get(key))
        except Exception:
            timestamp = None
        if timestamp is None:
            continue
        if 0 <= now - timestamp <= max_age:
            node_id = _node_from_state_key(prefix, key, suffix)
            if node_id:
                targets.add(node_id)
    return targets


def _queue_key(prefix: str, node_id: str, suffix: str) -> str:
    if suffix == "orch":
        return f"{prefix}:notify:{node_id}:orch"
    return f"{prefix}:{node_id}:{suffix}"


def inbox_depth(redis_client, prefix: str, node_id: str) -> int:
    try:
        return int(redis_client.llen(_queue_key(prefix, node_id, "inbox")) or 0)
    except Exception:
        return 0


def recent_drain_marker_targets(redis_client, prefix: str, now: float, max_age: float) -> set[str]:
    targets: set[str] = set()
    for suffix in READER_DRAIN_MARKER_SUFFIXES:
        targets.update(_scan_recent_timestamp_targets(redis_client, prefix, suffix, now, max_age))
    return targets


def recent_trace_drain_targets(redis_client, prefix: str, now: float, max_age: float) -> set[str]:
    targets: set[str] = set()
    streams = {f"{prefix}:notify_trace", "taey:notify_trace"}
    for stream in streams:
        try:
            entries = redis_client.xrevrange(stream, "+", "-", count=2000)
        except Exception:
            continue
        for _entry_id, fields in entries:
            if not isinstance(fields, dict):
                continue
            if _decode(fields.get("ev")) != "drain":
                continue
            timestamp = _float_or_none(fields.get("wall"))
            if timestamp is None or not (0 <= now - timestamp <= max_age):
                continue
            node_id = _decode(fields.get("node"))
            if node_id:
                targets.add(node_id)
    return targets


def reader_live_targets(redis_client, prefix: str) -> set[str]:
    return target_liveness_snapshot(redis_client, prefix).get("reader", set())


def _last_activity_age(redis_client, prefix: str, node_id: str, now: float) -> Optional[float]:
    try:
        timestamp = _float_or_none(redis_client.get(_state_key(prefix, node_id, "last_activity")))
    except Exception:
        timestamp = None
    if timestamp is None:
        return None
    return now - timestamp


def target_liveness_snapshot(
    redis_client,
    prefix: str,
    *,
    tmux_sessions: Optional[set[str]] = None,
    registered_sessions: Optional[set[str]] = None,
) -> dict[str, set[str]]:
    """Collect send-eligible reader targets and failure diagnostics."""
    now = time.time()
    max_age = _reader_liveness_window()
    tmux = set(tmux_sessions if tmux_sessions is not None else get_local_tmux_sessions())
    registered = set(
        registered_sessions if registered_sessions is not None else registered_session_targets()
    )
    draining = recent_drain_marker_targets(redis_client, prefix, now, max_age)
    draining.update(recent_trace_drain_targets(redis_client, prefix, now, max_age))
    reader: set[str] = set()
    queued_not_draining: set[str] = set()
    stale_activity: set[str] = set()
    for node_id in tmux:
        depth = inbox_depth(redis_client, prefix, node_id)
        if depth > 0 and node_id not in draining:
            queued_not_draining.add(node_id)
            continue
        age = _last_activity_age(redis_client, prefix, node_id, now)
        if age is None or age < 0 or age > max_age:
            stale_activity.add(node_id)
            continue
        reader.add(node_id)
    return {
        "reader": reader,
        "tmux": tmux,
        "registered": registered,
        "queued_not_draining": queued_not_draining,
        "stale_activity": stale_activity,
    }


def target_has_reader(snapshot: dict[str, set[str]], target: str) -> bool:
    node_id = str(target or "").strip()
    return node_id in snapshot.get("reader", set())


def _format_targets(values: set[str], limit: int) -> str:
    ordered = sorted(values)
    if not ordered:
        return "(none)"
    visible = ordered[:limit]
    suffix = "" if len(ordered) <= limit else f", ... (+{len(ordered) - limit} more)"
    return ", ".join(visible) + suffix


def format_target_liveness_failure(
    target: str,
    snapshot: dict[str, set[str]],
    *,
    redis_client=None,
    prefix: str = "taey",
    limit: int = 80,
) -> str:
    node_id = str(target or "").strip()
    now = time.time()
    max_age = _reader_liveness_window()
    depth = inbox_depth(redis_client, prefix, node_id) if redis_client is not None else -1
    activity_age = _last_activity_age(redis_client, prefix, node_id, now) if redis_client is not None else None
    draining = node_id in recent_drain_marker_targets(redis_client, prefix, now, max_age) if redis_client is not None else False
    if redis_client is not None:
        draining = draining or node_id in recent_trace_drain_targets(redis_client, prefix, now, max_age)
    has_session = node_id in snapshot.get("tmux", set())
    if not has_session:
        reason = "check 1 failed: tmux has-session is false"
    elif depth > 0 and not draining:
        reason = "check 2 failed: inbox is non-empty and not visibly draining"
    else:
        reason = "check 3 failed: last_activity is missing, stale, or from the future"
    return "\n".join([
        f"ERROR: target '{target}' failed notify readiness; refusing to enqueue.",
        f"Reason: {reason}.",
        "Required checks: (1) tmux session exists; (2) inbox depth is 0 or visibly draining; (3) last_activity is recent.",
        f"Target state: has_session={has_session} inbox_depth={depth} draining={draining} last_activity_age_seconds={activity_age if activity_age is not None else 'missing'} max_age_seconds={max_age:g}",
        "Live targets observed:",
        f"  eligible: {_format_targets(snapshot.get('reader', set()), limit)}",
        f"  tmux_sessions: {_format_targets(snapshot.get('tmux', set()), limit)}",
        f"  queued_not_draining: {_format_targets(snapshot.get('queued_not_draining', set()), limit)}",
        f"  stale_activity: {_format_targets(snapshot.get('stale_activity', set()), limit)}",
        f"  registered_diagnostic: {_format_targets(snapshot.get('registered', set()), limit)}",
        "Use --allow-unregistered-target only for intentional pre-provisioning sends.",
    ])


def validate_target_reader(
    redis_client,
    target: str,
    prefix: str,
    *,
    tmux_sessions: Optional[set[str]] = None,
    registered_sessions: Optional[set[str]] = None,
) -> tuple[bool, str]:
    snapshot = target_liveness_snapshot(
        redis_client,
        prefix,
        tmux_sessions=tmux_sessions,
        registered_sessions=registered_sessions,
    )
    if target_has_reader(snapshot, target):
        return True, ""
    return False, format_target_liveness_failure(
        target,
        snapshot,
        redis_client=redis_client,
        prefix=prefix,
    )
