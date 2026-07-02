#!/usr/bin/env python3
"""Notification delivery daemon — one per machine, handles all local tmux sessions.

The daemon is the stopped-session delivery path. It periodically discovers tmux
sessions, checks whether a session is safe to inject into, and submits a Redis
pointer via ``scripts/tmux-send``. Messages remain queued until the recipient hook
drains them after a real prompt/tool event.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# Ensure the repo root is importable when this file is executed as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hashlib
import json
import socket

from notifications.inbox import (
    WAKE_TYPES,
    flatten_sources,
    format_notification_block,
    has_pending_messages,
    inbox_key,
    is_node_idle,
    key_prefix,
    notifications_key,
    state_key,
)
from notifications.handoff import (
    mark_session_machine,
    record_delivery_signal,
    session_machine_key,
    validate_handoff_activation,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [notify-daemon] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 3
# Per-session pointer-inject backoff: a session whose safe-to-inject predicate
# stays true and whose inbox does NOT change (wedged / non-submitting / broken
# Stop hook) is re-injected at most once per this interval, instead of every poll_interval.
# A NEW message (inbox signature changes) injects promptly; a session that
# submits the pointer clears idle and is never re-injected. Prevents the
# every-3s keystroke hammer that disrupts wedged sessions fleet-wide.
POINTER_INJECT_BACKOFF_SECS = 90
HANDOFF_VALIDATION_TIMEOUT_SECS = 5.0
HANDOFF_VALIDATION_SHUTDOWN_GRACE_SECS = 0.1
REDIS_SOCKET_TIMEOUT_SECS = 2.0
DEFAULT_INJECT_FAILURE_ESCALATE_AFTER = 3
DEFAULT_INJECT_FAILURE_ESCALATION_TTL_SECS = 900
MAX_MESSAGE_LENGTH = 4000
DAEMON_HEARTBEAT_NODE = "_notify_daemon"
DAEMON_HEARTBEAT_SUFFIX = "heartbeat"
DAEMON_DELIVERY_PROGRESS_SUFFIX = "delivery_progress"
DEFAULT_DAEMON_HEARTBEAT_INTERVAL_SECS = 3.0
DEFAULT_DELIVERY_PROGRESS_MAX_AGE_SECS = 15.0
USAGE_LIMIT_IDLE_MARKERS = (
    "you've hit your session limit",
    "you have hit your session limit",
    "you've reached your session limit",
    "you have reached your session limit",
    "you've hit your weekly limit",
    "you have hit your weekly limit",
    "you've reached your weekly limit",
    "you have reached your weekly limit",
    "you've hit your usage limit",
    "you have hit your usage limit",
    "you've reached your usage limit",
    "you have reached your usage limit",
)
USAGE_LIMIT_TRANSIENT_EXCLUSIONS = (
    "not your usage limit",
)
USAGE_LIMIT_RESTING_REGION_NONBLANK_LINES = 3


class _HandoffValidationJob:
    def __init__(self, redis_client, *, prefix: str, timeout_sec: float):
        self.redis_client = redis_client
        self.prefix = prefix
        self.timeout_sec = timeout_sec
        self.started_at = time.monotonic()
        self.warned = False
        self.updated = 0
        self.error: Exception | None = None
        self.done = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name="notify-handoff-activation-validation",
            daemon=True,
        )

    def start(self) -> "_HandoffValidationJob":
        self.thread.start()
        return self

    def _run(self) -> None:
        try:
            self.updated = validate_handoff_activation(
                self.redis_client,
                prefix=self.prefix,
                timeout_sec=self.timeout_sec,
            )
        except Exception as exc:
            self.error = exc
        finally:
            self.done.set()


class _DaemonDeliveryProgress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cursor = 0
        self._last_monotonic = time.monotonic()

    def mark(self) -> int:
        with self._lock:
            self._cursor += 1
            self._last_monotonic = time.monotonic()
            return self._cursor

    def age(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_monotonic


def _consume_handoff_validation_job(
    job: _HandoffValidationJob | None,
    *,
    wait_sec: float = 0.0,
) -> _HandoffValidationJob | None:
    if job is None:
        return None
    if wait_sec > 0:
        job.thread.join(wait_sec)
    if not job.done.is_set():
        return job
    if job.error is not None:
        logger.error("handoff activation validation failed: %s", job.error)
    return None


def _advance_handoff_validation_job(
    job: _HandoffValidationJob | None,
    redis_client,
    *,
    prefix: str,
    timeout_sec: float,
) -> _HandoffValidationJob | None:
    job = _consume_handoff_validation_job(job)
    if job is None:
        return _HandoffValidationJob(redis_client, prefix=prefix, timeout_sec=timeout_sec).start()
    elapsed = time.monotonic() - job.started_at
    if not job.warned and elapsed >= timeout_sec:
        logger.error(
            "handoff activation validation still running after %.3fs; heartbeat/delivery continue",
            timeout_sec,
        )
        job.warned = True
    return job


def get_local_tmux_sessions() -> list[str]:
    """List all tmux sessions on this machine."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return [s.strip() for s in result.stdout.strip().splitlines() if s.strip()]
    except Exception:
        pass
    return []


def _tmux_pane_tail(session_name: str, *, lines: int = 80) -> str:
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session_name, "-S", f"-{lines}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout or ""
    except Exception as exc:
        logger.debug("tmux pane capture failed for %s: %s", session_name, exc)
    return ""


def _usage_limit_resting_region(pane_text: str) -> str:
    nonblank_lines = [line.strip() for line in (pane_text or "").splitlines() if line.strip()]
    return "\n".join(nonblank_lines[-USAGE_LIMIT_RESTING_REGION_NONBLANK_LINES:])


def _pane_shows_usage_limit_resting_state(pane_text: str) -> bool:
    normalized = " ".join(_usage_limit_resting_region(pane_text).lower().split())
    if not normalized:
        return False
    if any(marker in normalized for marker in USAGE_LIMIT_TRANSIENT_EXCLUSIONS):
        return False
    return any(marker in normalized for marker in USAGE_LIMIT_IDLE_MARKERS)


def reconcile_usage_limit_idle(r, node_id: str, session_name: str) -> bool:
    """Restore idle=1 when Claude Code parks at a usage-limit banner.

    A session-limit abort can return the TUI to a resting prompt without firing
    Stop, leaving the explicit idle flag absent. This helper repairs only that
    observed parked state; normal idle-absent sessions remain active and wait
    for PostToolUse/UserPromptSubmit delivery.
    """
    if is_node_idle(r, node_id):
        return False
    if r.exists(state_key(node_id, "tool_running")):
        return False
    if not _pane_shows_usage_limit_resting_state(_tmux_pane_tail(session_name)):
        return False
    r.set(state_key(node_id, "idle"), "1")
    logger.warning("Reconciled idle=1 for %s after usage-limit banner", node_id)
    try:
        from notifications.trace import trace
        trace(r, "idle_set", node=node_id, src="usage_limit_reconcile")
    except Exception:
        pass
    return True


def _clip_message(message: str) -> str:
    if len(message) <= MAX_MESSAGE_LENGTH:
        return message
    return message[: MAX_MESSAGE_LENGTH - 28].rstrip() + "\n...[notification block truncated]"


def inject_via_tmux(session_name: str, message: str) -> bool:
    """Inject a message into a local Claude Code tmux session."""
    tmux_send = Path(__file__).resolve().parent.parent / "scripts" / "tmux-send"

    if tmux_send.exists():
        cmd = ["bash", str(tmux_send), "local", session_name, message]
    else:
        # Fallback for stripped-down environments.
        cmd = ["tmux", "send-keys", "-t", session_name, "--", message, "Enter"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            logger.error(
                "tmux injection failed for %s: %s",
                session_name,
                (result.stderr or result.stdout).strip(),
            )
            return False
        return True
    except Exception as exc:
        logger.error("tmux injection exception for %s: %s", session_name, exc)
        return False


def _resolve_supervisor(r, node_id: str) -> str | None:
    from notifications.targets import resolve_supervisor

    return resolve_supervisor(r, node_id)


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def _int_or_zero(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def write_daemon_heartbeat(r, machine: str, *, now: float | None = None) -> str:
    ts = time.time() if now is None else float(now)
    value = f"{ts:.6f}+{machine}"
    r.set(state_key(DAEMON_HEARTBEAT_NODE, DAEMON_HEARTBEAT_SUFFIX), value)
    return value


def write_daemon_delivery_progress(
    r,
    machine: str,
    *,
    cursor: int,
    now: float | None = None,
) -> str:
    ts = time.time() if now is None else float(now)
    value = f"{ts:.6f}+{machine}+{int(cursor)}"
    r.set(state_key(DAEMON_HEARTBEAT_NODE, DAEMON_DELIVERY_PROGRESS_SUFFIX), value)
    return value


def _connect_redis(redis_lib, redis_host: str, redis_port: int):
    return redis_lib.Redis(
        host=redis_host,
        port=redis_port,
        decode_responses=True,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECS,
        socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECS,
    )


def _mark_daemon_delivery_progress(
    r,
    progress: _DaemonDeliveryProgress,
    machine: str,
) -> None:
    cursor = progress.mark()
    try:
        write_daemon_delivery_progress(r, machine, cursor=cursor, now=time.time())
    except Exception as exc:
        logger.error("daemon delivery progress write failed: %s", exc)


def _start_daemon_heartbeat_thread(
    redis_lib,
    redis_host: str,
    redis_port: int,
    machine: str,
    *,
    interval_sec: float,
    delivery_progress: _DaemonDeliveryProgress,
    delivery_progress_max_age_sec: float,
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    heartbeat_redis = _connect_redis(redis_lib, redis_host, redis_port)

    def beat() -> None:
        stalled_logged = False
        while not stop_event.is_set():
            progress_age = delivery_progress.age()
            if progress_age > delivery_progress_max_age_sec:
                if not stalled_logged:
                    logger.critical(
                        "daemon delivery loop progress stalled for %.3fs; heartbeat withheld",
                        progress_age,
                    )
                    stalled_logged = True
                stop_event.wait(interval_sec)
                continue
            stalled_logged = False
            try:
                write_daemon_heartbeat(heartbeat_redis, machine, now=time.time())
            except Exception as exc:
                logger.error("daemon heartbeat write failed: %s", exc)
            stop_event.wait(interval_sec)

    try:
        write_daemon_heartbeat(heartbeat_redis, machine, now=time.time())
    except Exception as exc:
        logger.error("daemon heartbeat write failed: %s", exc)

    thread = threading.Thread(
        target=beat,
        name="notify-daemon-heartbeat",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _record_pointer_inject_result(r, node_id: str, *, ok: bool,
                                  machine: str, now: float) -> None:
    count_key = state_key(node_id, "pointer_inject_fail_count")
    notice_key = state_key(node_id, "pointer_inject_failure_notified")
    if ok:
        r.delete(count_key, notice_key)
        return

    ttl_secs = _int_env(
        "INJECT_FAILURE_ESCALATION_TTL_SECS",
        DEFAULT_INJECT_FAILURE_ESCALATION_TTL_SECS,
    )
    count = _int_or_zero(r.get(count_key)) + 1
    r.set(count_key, str(count), ex=ttl_secs)

    threshold = _int_env(
        "INJECT_FAILURE_ESCALATE_AFTER",
        DEFAULT_INJECT_FAILURE_ESCALATE_AFTER,
    )
    if count < threshold or r.exists(notice_key):
        return

    supervisor = _resolve_supervisor(r, node_id)
    body = (
        f"{node_id} pointer injection failed {count} consecutive times on {machine}; "
        "messages remain queued in Redis."
    )
    if supervisor and supervisor != node_id:
        msg = json.dumps({
            "from": node_id,
            "type": "inject_failure",
            "body": body,
            "priority": "high",
            "msg_id": f"inject-failure-{node_id}-{int(now)}",
            "timestamp": now,
            "failure_count": count,
            "machine": machine,
        })
        r.lpush(inbox_key(str(supervisor)), msg)
        logger.error("Pointer injection failure escalation: %s -> %s", node_id, supervisor)
    else:
        logger.error("Pointer injection failure escalation: %s", body)
    r.set(notice_key, "1", ex=ttl_secs)


def build_pointer_summary(r, node_id: str) -> str | None:
    """Peek inbox + notifications, return a short summary pointing to Redis.

    Does NOT pop messages. The recipient reads them via the PostToolUse hook
    on their next tool call, which displays full content via additionalContext.
    """
    inbox = inbox_key(node_id)
    notif_key = notifications_key(node_id)

    inbox_raw = r.lrange(inbox, -10, -1)  # tail = oldest first
    notif_raw = r.lrange(notif_key, 0, 9)

    total = r.llen(inbox) + r.llen(notif_key)
    if total == 0:
        return None

    wake_messages = []
    for raw in reversed(inbox_raw):
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if msg.get("wake_type") in WAKE_TYPES:
            wake_messages.append(msg)
    for raw in notif_raw:
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if msg.get("wake_type") in WAKE_TYPES:
            wake_messages.append(msg)
    if wake_messages:
        return _clip_message(
            format_notification_block(
                wake_messages,
                header="=== WAKE ===",
            )
        )

    senders = set()
    first_type = None

    for raw in notif_raw:
        try:
            msg = json.loads(raw)
            senders.add(msg.get('platform', msg.get('from', 'unknown')))
            if not first_type:
                first_type = msg.get('status', msg.get('type', 'message')).upper()
        except Exception:
            pass

    for raw in reversed(inbox_raw):
        try:
            msg = json.loads(raw)
            senders.add(msg.get('from', 'unknown'))
            if not first_type:
                first_type = msg.get('type', 'message').upper()
        except Exception:
            pass

    if not first_type:
        first_type = "MESSAGE"

    sender_list = sorted(senders)
    senders_str = ", ".join(sender_list[:3]) + (", ..." if len(sender_list) > 3 else "")

    return (
        f"[NOTIFY] You have {total} messages (from {senders_str}; first={first_type}). "
        f"Read with: redis-cli -h 127.0.0.1 LRANGE {inbox} 0 -1 "
        f"(or wait — your PostToolUse hook will surface them on the next tool call)."
    )


def build_grok_full_body(r, node_id: str) -> str | None:
    """Build the full-body injection text for grok-cli targets.

    Why grok is special (validated 2026-05-26 by x-claude + treasurer
    independently with 3-fact chains): grok-cli's prompt-time hook
    inheritance (via ~/.claude/settings.json) DOES fire prompt_activity.py
    + DOES drain the Redis inbox, BUT grok-cli does NOT honor the
    ``additionalContext`` field from the hook's JSON return value the way
    Claude Code does. Net: the drained message bodies never reach grok's
    LLM context; grok only sees the pointer text injected by tmux-send.

    Workaround: for grok-named targets, the daemon injects the FULL
    message bodies via tmux-send instead of the pointer. Grok then sees
    the actual content as its user prompt. The hook still drains the
    inbox on grok's submit (idempotent — bodies were already delivered
    inline). The Ctrl-U+Ctrl-K pre-clear in tmux-send v1.0.1 prevents
    accumulation if the daemon polls before grok submits.

    Returns the concatenated bodies (each prefixed with its from + type)
    or None if there's nothing to deliver. Caps total size at ~6KB to
    avoid pathological tmux injections; truncated bodies get a clear
    ``[... truncated, read full at <key>]`` suffix pointing at Redis.
    """
    inbox = inbox_key(node_id)
    notif_key = notifications_key(node_id)

    # Peek tail-first (oldest first) — same convention as build_pointer_summary.
    raw_msgs = list(reversed(r.lrange(inbox, -10, -1)))
    raw_msgs.extend(r.lrange(notif_key, 0, 9))
    if not raw_msgs:
        return None

    blocks = []
    total_bytes = 0
    MAX_BYTES = 6000

    for raw in raw_msgs:
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        sender = msg.get('from', msg.get('platform', 'unknown'))
        mtype = msg.get('type', msg.get('status', 'message')).upper()
        body = msg.get('body', '')
        if not body:
            continue
        block = f"[{mtype} from {sender}]:\n{body}"
        if total_bytes + len(block) > MAX_BYTES:
            block = (block[: MAX_BYTES - total_bytes - 80].rstrip()
                     + f"\n[... message truncated; read full at redis key {inbox}]")
            blocks.append(block)
            break
        blocks.append(block)
        total_bytes += len(block)

    if not blocks:
        return None
    return "\n\n".join(blocks)


def run_daemon(
    redis_host: str,
    redis_port: int = 6379,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
) -> None:
    """Main loop — one process handles all local tmux sessions."""
    import redis as redis_lib

    r = _connect_redis(redis_lib, redis_host, redis_port)

    try:
        r.ping()
    except Exception as exc:
        logger.error("Cannot connect to Redis at %s:%s: %s", redis_host, redis_port, exc)
        sys.exit(1)

    machine = socket.gethostname()
    logger.info(
        "Started on %s: redis=%s:%s poll=%ss",
        machine,
        redis_host,
        redis_port,
        poll_interval,
    )
    logger.info("Using Redis key prefix: %s", key_prefix())

    handoff_validation_job: _HandoffValidationJob | None = None
    delivery_progress = _DaemonDeliveryProgress()
    delivery_progress.mark()
    heartbeat_stop, heartbeat_thread = _start_daemon_heartbeat_thread(
        redis_lib,
        redis_host,
        redis_port,
        machine,
        interval_sec=max(0.01, min(float(poll_interval), DEFAULT_DAEMON_HEARTBEAT_INTERVAL_SECS)),
        delivery_progress=delivery_progress,
        delivery_progress_max_age_sec=max(
            DEFAULT_DELIVERY_PROGRESS_MAX_AGE_SECS,
            float(poll_interval) * 3,
        ),
    )
    try:
        while True:
            try:
                local_sessions = get_local_tmux_sessions()
                local_session_set = set(local_sessions)
                _mark_daemon_delivery_progress(r, delivery_progress, machine)

                try:
                    handoff_validation_job = _advance_handoff_validation_job(
                        handoff_validation_job,
                        r,
                        prefix=key_prefix(),
                        timeout_sec=HANDOFF_VALIDATION_TIMEOUT_SECS,
                    )
                except Exception as exc:
                    logger.error("handoff activation validation failed: %s", exc)

                for session_name in local_sessions:
                    node_id = session_name
                    _mark_daemon_delivery_progress(r, delivery_progress, machine)
                    try:
                        mark_session_machine(
                            r,
                            prefix=key_prefix(),
                            session_id=node_id,
                            machine=machine,
                        )
                    except Exception:
                        pass

                    if not has_pending_messages(r, node_id):
                        continue
                    now = time.time()
                    if not is_node_idle(r, node_id):
                        reconcile_usage_limit_idle(r, node_id, session_name)
                    if not is_node_idle(r, node_id):
                        continue

                    # Per-session inject backoff. The inbox signature changes when a
                    # new message arrives (inject promptly) but stays identical for a
                    # wedged/non-submitting session (idle never clears) — in which
                    # case re-inject at most once per POINTER_INJECT_BACKOFF_SECS
                    # instead of every poll. Prevents the keystroke hammer.
                    try:
                        inbox_sig = hashlib.sha1(
                            b"\n".join(s.encode() for s in r.lrange(inbox_key(node_id), 0, -1))
                        ).hexdigest()
                    except Exception:
                        inbox_sig = None
                    if inbox_sig is not None:
                        bo_raw = r.get(state_key(node_id, "pointer_inject_backoff"))
                        if bo_raw:
                            try:
                                bo = json.loads(bo_raw)
                                if (bo.get("sig") == inbox_sig
                                        and (now - float(bo.get("ts", 0))) < POINTER_INJECT_BACKOFF_SECS):
                                    continue  # same undrained inbox, injected recently -> back off
                            except Exception:
                                pass

                    # Grok-cli does NOT honor additionalContext from prompt hooks
                    # the way Claude Code / codex / gemini do. For *-grok targets
                    # we inject FULL message bodies via tmux instead of the
                    # pointer; for everyone else we keep the pointer pattern.
                    # Inbox still drains via the hook on grok's submit
                    # (idempotent — bodies already delivered inline).
                    # Verified 2026-05-26 by x-claude + treasurer independently
                    # with 3-fact chains showing the pointer pattern silently
                    # dropped grok's dispatched bodies.
                    if node_id.endswith("-grok"):
                        summary = build_grok_full_body(r, node_id)
                    else:
                        # POINTER ONLY — peek inbox, build summary, inject into tmux.
                        # Full bodies stay in Redis; recipient reads via hook on
                        # next tool call / prompt submit.
                        summary = build_pointer_summary(r, node_id)
                    if not summary:
                        continue

                    # DO NOT clear idle here. idle is removed ONLY by the
                    # UserPromptSubmit hook — i.e. only when the recipient actually
                    # SUBMITS the injected pointer as a prompt (= real, validated
                    # delivery). Clearing it here ASSUMED the injection landed; when an
                    # injection silently failed, idle was gone with no prompt ever
                    # submitted, so the message stranded with nothing to re-trigger
                    # delivery.
                    #
                    # Leaving idle set: a failed / unsubmitted injection simply retries
                    # on the next poll; a SUCCESSFUL one is cleared by the hook at
                    # submit-time (within one poll window), so it does not re-inject.
                    # Double-delivery is therefore prevented by the hook, not by the
                    # daemon pre-emptively clearing a flag it cannot validate.
                    logger.info("Notifying %s: %s", session_name, summary)
                    _mark_daemon_delivery_progress(r, delivery_progress, machine)
                    ok = inject_via_tmux(session_name, summary)
                    _mark_daemon_delivery_progress(r, delivery_progress, machine)
                    # Stamp the backoff on every attempt (ok or not): whether the
                    # inject lands or fails, do not re-inject this same inbox for
                    # POINTER_INJECT_BACKOFF_SECS. A new message changes inbox_sig and
                    # injects promptly; a successful submit clears idle (no re-inject).
                    if inbox_sig is not None:
                        try:
                            r.set(
                                state_key(node_id, "pointer_inject_backoff"),
                                json.dumps({"sig": inbox_sig, "ts": now}),
                                ex=POINTER_INJECT_BACKOFF_SECS * 4,
                            )
                        except Exception:
                            pass
                    try:
                        from notifications.trace import trace
                        trace(r, "inject", node=session_name, ok=ok)
                    except Exception:
                        pass
                    try:
                        record_delivery_signal(
                            r,
                            prefix=key_prefix(),
                            target_session_id=node_id,
                            signal="inject_ok" if ok else "inject_failed",
                            signal_source="notify-daemon",
                            machine=machine,
                        )
                    except Exception:
                        pass
                    try:
                        _record_pointer_inject_result(
                            r,
                            node_id,
                            ok=ok,
                            machine=machine,
                            now=now,
                        )
                    except Exception:
                        pass
                    if not ok:
                        logger.error(
                            "Injection failed for %s; message left queued, will retry",
                            session_name,
                        )

                inbox_pattern = f"{key_prefix()}:*:inbox"
                for inbox in r.scan_iter(match=inbox_pattern):
                    _mark_daemon_delivery_progress(r, delivery_progress, machine)
                    parts = str(inbox).split(":")
                    if len(parts) < 3:
                        continue
                    target_session_id = parts[-2]
                    if target_session_id in local_session_set:
                        continue
                    if not has_pending_messages(r, target_session_id):
                        continue
                    machine_key = session_machine_key(key_prefix(), target_session_id)
                    if r.get(machine_key) != machine:
                        continue
                    try:
                        record_delivery_signal(
                            r,
                            prefix=key_prefix(),
                            target_session_id=target_session_id,
                            signal="tmux_missing",
                            signal_source="notify-daemon",
                            machine=machine,
                        )
                    except Exception:
                        pass

                try:
                    from notifications.trace import trim_trace
                    trim_trace(r)
                except Exception:
                    pass
                time.sleep(poll_interval)
            except KeyboardInterrupt:
                logger.info("Daemon stopped")
                break
            except Exception as exc:
                logger.error("Error: %s", exc)
                time.sleep(poll_interval)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(1.0)
        _consume_handoff_validation_job(
            handoff_validation_job,
            wait_sec=HANDOFF_VALIDATION_SHUTDOWN_GRACE_SECS,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Notification delivery daemon (one per machine)")
    parser.add_argument("--redis-host", default=os.environ.get("REDIS_HOST", "127.0.0.1"))
    parser.add_argument("--redis-port", type=int, default=int(os.environ.get("REDIS_PORT", "6379")))
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL)
    # Legacy args (ignored — kept for backwards compatibility with old scripts)
    parser.add_argument("--node", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--tmux-session", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    run_daemon(args.redis_host, args.redis_port, args.poll_interval)


if __name__ == "__main__":
    main()
