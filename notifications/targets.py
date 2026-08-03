"""Notify target resolution helpers."""
from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any, Optional


PEER_SUFFIXES = ("-codex", "-gemini", "-grok")
READER_DRAIN_MARKER_SUFFIXES = ("last_drain_at", "last_read_at")
READER_STATE_SUFFIXES = ("last_activity", "idle", "turns_open", "seat_registration")
TAEY_LINE_READER_RE = re.compile(r"^taey(?:-council-[1-7])?$")


class TargetReadinessError(RuntimeError):
    """Raised when a target readiness signal cannot be measured."""


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


def _local_tmux_sessions_probe() -> tuple[set[str], Optional[str]]:
    """Return local tmux session names plus a probe error, if tmux is unknown."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception as exc:
        return set(), f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return set(), f"tmux list-sessions exited {result.returncode}: {detail}"
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}, None


def get_local_tmux_sessions() -> set[str]:
    """Return local tmux session names, or an empty set when tmux is unavailable."""
    sessions, _probe_error = _local_tmux_sessions_probe()
    return sessions


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


def _int_or_none(raw) -> Optional[int]:
    try:
        return int(_decode(raw))
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
    except Exception as exc:
        raise TargetReadinessError(
            f"cannot read {prefix}:{node_id}:inbox depth: {exc}"
        ) from exc


def _redis_key_exists(redis_client, key: str) -> bool:
    try:
        return bool(redis_client.exists(key))
    except Exception as exc:
        raise TargetReadinessError(f"cannot read {key} existence: {exc}") from exc


def _turns_open(redis_client, prefix: str, node_id: str) -> Optional[int]:
    key = _state_key(prefix, node_id, "turns_open")
    try:
        raw = redis_client.get(key)
    except Exception as exc:
        raise TargetReadinessError(f"cannot read {key}: {exc}") from exc
    if raw in (None, "", b""):
        return None
    value = _int_or_none(raw)
    if value is None:
        raise TargetReadinessError(f"cannot parse {key}: {_decode(raw)!r}")
    return value


def _is_taey_line_reader(node_id: str) -> bool:
    return bool(TAEY_LINE_READER_RE.fullmatch(str(node_id or "").strip()))


def _state_signal_targets(redis_client, prefix: str) -> set[str]:
    targets: set[str] = set()
    for suffix in READER_STATE_SUFFIXES:
        for key in _redis_scan(redis_client, f"{prefix}:*:{suffix}"):
            node_id = _node_from_state_key(prefix, key, suffix)
            if node_id:
                targets.add(node_id)
    return targets


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
    except Exception as exc:
        raise TargetReadinessError(
            f"cannot read {prefix}:{node_id}:last_activity: {exc}"
        ) from exc
    if timestamp is None:
        return None
    return now - timestamp


def target_liveness_snapshot(
    redis_client,
    prefix: str,
    *,
    tmux_sessions: Optional[set[str]] = None,
    registered_sessions: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Collect send-eligible reader targets and failure diagnostics."""
    now = time.time()
    max_age = _reader_liveness_window()
    tmux_probe_error: Optional[str] = None
    if tmux_sessions is None:
        tmux, tmux_probe_error = _local_tmux_sessions_probe()
    else:
        tmux = set(tmux_sessions)
    registered = set(
        registered_sessions if registered_sessions is not None else registered_session_targets()
    )
    draining = recent_drain_marker_targets(redis_client, prefix, now, max_age)
    draining.update(recent_trace_drain_targets(redis_client, prefix, now, max_age))
    state_signal = _state_signal_targets(redis_client, prefix)
    taey_line_readers = {"taey", *(f"taey-council-{idx}" for idx in range(1, 8))}
    candidates = tmux | registered | state_signal | taey_line_readers
    reader: set[str] = set()
    headless_reader: set[str] = set()
    idle_ready: set[str] = set()
    probe_unknown_allowed: set[str] = set()
    missing_reader_signal: set[str] = set()
    queued_not_draining: set[str] = set()
    unreadable_inbox: set[str] = set()
    stale_activity: set[str] = set()
    inbox_depths: dict[str, int] = {}
    depth_errors: dict[str, str] = {}
    activity_ages: dict[str, float | None] = {}
    activity_errors: dict[str, str] = {}
    state_errors: dict[str, str] = {}
    turns_open_by_target: dict[str, int | None] = {}
    for node_id in candidates:
        has_tmux_session = node_id in tmux
        registered_target = node_id in registered
        has_state_signal = node_id in state_signal
        is_taey_line_reader = _is_taey_line_reader(node_id)
        try:
            depth = inbox_depth(redis_client, prefix, node_id)
        except TargetReadinessError as exc:
            unreadable_inbox.add(node_id)
            depth_errors[node_id] = str(exc)
            continue
        inbox_depths[node_id] = depth
        try:
            idle_flag = _redis_key_exists(redis_client, _state_key(prefix, node_id, "idle"))
            seat_registered = _redis_key_exists(redis_client, _state_key(prefix, node_id, "seat_registration"))
            turns_open = _turns_open(redis_client, prefix, node_id)
        except TargetReadinessError as exc:
            stale_activity.add(node_id)
            state_errors[node_id] = str(exc)
            continue
        turns_open_by_target[node_id] = turns_open
        queue_drained_or_draining = depth == 0 or node_id in draining
        idle_drained = idle_flag and depth == 0 and (turns_open is None or turns_open == 0)
        if idle_drained:
            idle_ready.add(node_id)
        try:
            age = _last_activity_age(redis_client, prefix, node_id, now)
        except TargetReadinessError as exc:
            stale_activity.add(node_id)
            activity_errors[node_id] = str(exc)
            continue
        activity_ages[node_id] = age
        fresh_activity = age is not None and 0 <= age <= max_age
        headless_identity = (
            not has_tmux_session
            and (registered_target or is_taey_line_reader or seat_registered)
        )
        headless_ready = (
            headless_identity
            and queue_drained_or_draining
            and (fresh_activity or node_id in draining)
        )
        if depth > 0 and node_id not in draining and not headless_ready:
            queued_not_draining.add(node_id)
            continue
        if not has_tmux_session and headless_ready:
            headless_reader.add(node_id)
        if has_tmux_session or headless_ready:
            if fresh_activity or idle_drained or headless_ready:
                reader.add(node_id)
                continue
        if tmux_probe_error and (registered_target or has_state_signal or is_taey_line_reader):
            probe_unknown_allowed.add(node_id)
            reader.add(node_id)
            continue
        if not (has_tmux_session or registered_target or is_taey_line_reader or seat_registered or has_state_signal):
            missing_reader_signal.add(node_id)
            continue
        if age is None or age < 0 or age > max_age:
            stale_activity.add(node_id)
            continue
    return {
        "reader": reader,
        "tmux": tmux,
        "registered": registered,
        "state_signal": state_signal,
        "headless_reader": headless_reader,
        "idle_ready": idle_ready,
        "probe_unknown_allowed": probe_unknown_allowed,
        "missing_reader_signal": missing_reader_signal,
        "tmux_probe_error": tmux_probe_error,
        "draining": draining,
        "queued_not_draining": queued_not_draining,
        "unreadable_inbox": unreadable_inbox,
        "stale_activity": stale_activity,
        "inbox_depths": inbox_depths,
        "depth_errors": depth_errors,
        "activity_ages": activity_ages,
        "activity_errors": activity_errors,
        "state_errors": state_errors,
        "turns_open": turns_open_by_target,
    }


def target_has_reader(snapshot: dict[str, Any], target: str) -> bool:
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
    snapshot: dict[str, Any],
    *,
    limit: int = 80,
) -> str:
    node_id = str(target or "").strip()
    max_age = _reader_liveness_window()
    depth_errors = snapshot.get("depth_errors", {})
    activity_errors = snapshot.get("activity_errors", {})
    state_errors = snapshot.get("state_errors", {})
    inbox_depths = snapshot.get("inbox_depths", {})
    activity_ages = snapshot.get("activity_ages", {})
    turns_open_by_target = snapshot.get("turns_open", {})
    depth_error = depth_errors.get(node_id, "") if isinstance(depth_errors, dict) else ""
    activity_error = activity_errors.get(node_id, "") if isinstance(activity_errors, dict) else ""
    state_error = state_errors.get(node_id, "") if isinstance(state_errors, dict) else ""
    depth = inbox_depths.get(node_id, "unreadable" if depth_error else "not_checked") if isinstance(inbox_depths, dict) else "not_checked"
    activity_age = activity_ages.get(node_id, None) if isinstance(activity_ages, dict) else None
    turns_open = turns_open_by_target.get(node_id, "missing") if isinstance(turns_open_by_target, dict) else "missing"
    draining = node_id in snapshot.get("draining", set())
    has_session = node_id in snapshot.get("tmux", set())
    has_headless = node_id in snapshot.get("headless_reader", set())
    idle_ready = node_id in snapshot.get("idle_ready", set())
    probe_unknown = node_id in snapshot.get("probe_unknown_allowed", set())
    registered = node_id in snapshot.get("registered", set())
    state_signal = node_id in snapshot.get("state_signal", set())
    tmux_probe_error = snapshot.get("tmux_probe_error")
    has_reader_signal = (
        has_session
        or has_headless
        or idle_ready
        or registered
        or state_signal
        or _is_taey_line_reader(node_id)
        or probe_unknown
    )
    if not has_reader_signal:
        reason = "check 1 failed: no tmux/headless reader signal"
        if tmux_probe_error:
            reason += f"; local tmux probe unavailable ({tmux_probe_error})"
    elif depth_error:
        reason = f"check 2 failed: inbox depth unreadable ({depth_error})"
    elif isinstance(depth, int) and depth > 0 and not draining:
        reason = "check 2 failed: inbox is non-empty and not visibly draining"
    elif activity_error:
        reason = f"check 3 failed: last_activity unreadable ({activity_error})"
    elif state_error:
        reason = f"check 3 failed: reader state unreadable ({state_error})"
    else:
        reason = "check 3 failed: no fresh activity, idle-drained state, or headless fresh/drain evidence"
    return "\n".join([
        f"ERROR: target '{target}' failed notify readiness; refusing to enqueue.",
        f"Reason: {reason}.",
        "Required checks: (1) reader signal exists via tmux or first-class headless presence; (2) queued tmux mail is 0 or visibly draining, while headless presence is the queue-consumer signal; (3) reader is active, explicitly idle, or headless-present.",
        f"Target state: has_tmux={has_session} headless={has_headless} registered={registered} state_signal={state_signal} inbox_depth={depth} draining={draining} idle_ready={idle_ready} turns_open={turns_open} last_activity_age_seconds={activity_age if activity_age is not None else 'missing'} max_age_seconds={max_age:g}",
        "Live targets observed:",
        f"  eligible: {_format_targets(snapshot.get('reader', set()), limit)}",
        f"  tmux_sessions: {_format_targets(snapshot.get('tmux', set()), limit)}",
        f"  headless_reader: {_format_targets(snapshot.get('headless_reader', set()), limit)}",
        f"  idle_ready: {_format_targets(snapshot.get('idle_ready', set()), limit)}",
        f"  probe_unknown_allowed: {_format_targets(snapshot.get('probe_unknown_allowed', set()), limit)}",
        f"  queued_not_draining: {_format_targets(snapshot.get('queued_not_draining', set()), limit)}",
        f"  unreadable_inbox: {_format_targets(snapshot.get('unreadable_inbox', set()), limit)}",
        f"  stale_activity: {_format_targets(snapshot.get('stale_activity', set()), limit)}",
        f"  registered_diagnostic: {_format_targets(snapshot.get('registered', set()), limit)}",
        f"  tmux_probe_error: {tmux_probe_error or '(none)'}",
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
    try:
        snapshot = target_liveness_snapshot(
            redis_client,
            prefix,
            tmux_sessions=tmux_sessions,
            registered_sessions=registered_sessions,
        )
    except Exception as exc:
        return False, "\n".join([
            f"ERROR: target '{target}' failed notify readiness; refusing to enqueue.",
            f"Reason: readiness check failed closed: {exc}.",
            "Use --allow-unregistered-target only for intentional pre-provisioning sends.",
        ])
    if target_has_reader(snapshot, target):
        return True, ""
    return False, format_target_liveness_failure(target, snapshot)
