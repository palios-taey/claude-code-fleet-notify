#!/usr/bin/env python3
"""Notification delivery daemon — one per machine, handles all local tmux sessions.

The daemon is the idle delivery path. It periodically discovers tmux sessions, checks
whether a session is safe to inject into, drains any queued notifications, and submits
those notifications via ``scripts/tmux-send``. If injection fails, the drained payload is
re-queued in FIFO order so no messages are lost.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
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
from notifications.task_liveness import peer_idle_allowed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [notify-daemon] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 3
# Per-session pointer-inject backoff: a session whose idle flag stays set and
# whose inbox does NOT change (wedged / non-submitting / broken Stop hook) is
# re-injected at most once per this interval, instead of every poll_interval.
# A NEW message (inbox signature changes) injects promptly; a session that
# submits the pointer clears idle and is never re-injected. Prevents the
# every-3s keystroke hammer that disrupts wedged sessions fleet-wide.
POINTER_INJECT_BACKOFF_SECS = 90
MAX_MESSAGE_LENGTH = 4000
DEFAULT_PEER_INACTIVE_STALE_SECS = 300
PEER_INACTIVE_DEDUP_SECS = 300


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
    for suffix in ("-codex", "-gemini", "-grok"):
        if node_id.endswith(suffix):
            suffix_supervisor = node_id[: -len(suffix)]
            explicit = r.get(state_key(node_id, "parent"))
            if explicit and explicit != node_id:
                return explicit
            return suffix_supervisor
    explicit = r.get(state_key(node_id, "parent"))
    if explicit:
        return explicit
    return None


def _decode_current_task(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        task = json.loads(raw)
    except Exception:
        return None
    return task if isinstance(task, dict) else None


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def notify_dispatcher_if_peer_inactive(r, node_id: str, *, now: float | None = None,
                                       stale_secs: int = DEFAULT_PEER_INACTIVE_STALE_SECS) -> bool:
    """Backstop for stalls where the CLI never fires Stop/AfterAgent.

    If a local peer still holds dispatch context but has stale tool activity,
    enqueue one peer_idle-style lifecycle message to its dispatcher. This is
    intentionally independent of the explicit idle flag, because the missing
    hook is the failure mode this backstop covers.
    """
    if r.exists(state_key(node_id, "tool_running")):
        return False
    task = _decode_current_task(r.get(state_key(node_id, "current_task")))
    if not task:
        return False
    task_id = task.get("task_id")
    supervisor = _resolve_supervisor(r, node_id) or task.get("supervisor")
    allowed, reason, _ = peer_idle_allowed(task_id, node_id, supervisor)
    if not allowed:
        logger.info(
            "Skipping backstop peer_idle for %s task=%s: %s",
            node_id,
            task_id or "?",
            reason,
        )
        return False
    timestamps = [
        _float_or_none(r.get(state_key(node_id, "last_tool_activity"))),
        _float_or_none(r.get(state_key(node_id, "last_activity"))),
        _float_or_none(task.get("started_at")),
    ]
    timestamps = [ts for ts in timestamps if ts is not None]
    if not timestamps:
        return False
    now = time.time() if now is None else now
    timestamp = max(timestamps)
    stale_for = max(0, int(now - timestamp))
    if stale_for < stale_secs:
        return False

    dedup_suffix = task_id or "no-task"
    dedup = f"{key_prefix()}:peer-inactive-notified:{node_id}:{dedup_suffix}"
    if r.exists(dedup):
        return False

    body = (
        f"{node_id} inactive for {stale_for}s while holding dispatched task "
        f"{task_id or '?'}"
    )
    msg = json.dumps({
        "from": node_id,
        "type": "peer_idle",
        "body": body,
        "outcome": "unknown",
        "priority": "high",
        "msg_id": f"peer-inactive-{node_id}-{dedup_suffix}-{int(now)}",
        "timestamp": now,
        "task_id": task_id,
        "task_description": task.get("description"),
        "task_supervisor": task.get("supervisor"),
        "task_started_at": task.get("started_at"),
        "inactive_for_sec": stale_for,
        "backstop": "stale_last_tool_activity",
    })
    r.lpush(inbox_key(str(supervisor)), msg)
    r.set(dedup, "1", ex=PEER_INACTIVE_DEDUP_SECS)
    logger.warning("Backstop peer_idle: %s -> %s (%s)", node_id, supervisor, body)
    return True


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

    r = redis_lib.Redis(
        host=redis_host,
        port=redis_port,
        decode_responses=True,
        socket_timeout=2,
    )

    try:
        r.ping()
    except Exception as exc:
        logger.error("Cannot connect to Redis at %s:%s: %s", redis_host, redis_port, exc)
        sys.exit(1)

    import socket

    logger.info(
        "Started on %s: redis=%s:%s poll=%ss",
        socket.gethostname(),
        redis_host,
        redis_port,
        poll_interval,
    )
    logger.info("Using Redis key prefix: %s", key_prefix())

    while True:
        try:
            local_sessions = get_local_tmux_sessions()
            local_session_set = set(local_sessions)
            machine = socket.gethostname()
            peer_inactive_stale_secs = int(
                os.environ.get("CF_PEER_INACTIVE_STALE_SECS", str(DEFAULT_PEER_INACTIVE_STALE_SECS))
            )

            try:
                validate_handoff_activation(r, prefix=key_prefix())
            except Exception as exc:
                logger.error("handoff activation validation failed: %s", exc)

            for session_name in local_sessions:
                node_id = session_name
                try:
                    mark_session_machine(
                        r,
                        prefix=key_prefix(),
                        session_id=node_id,
                        machine=machine,
                    )
                except Exception:
                    pass

                notify_dispatcher_if_peer_inactive(
                    r,
                    node_id,
                    stale_secs=peer_inactive_stale_secs,
                )

                if not is_node_idle(r, node_id):
                    continue
                if not has_pending_messages(r, node_id):
                    continue

                # Per-session inject backoff. The inbox signature changes when a
                # new message arrives (inject promptly) but stays identical for a
                # wedged/non-submitting session (idle never clears) — in which
                # case re-inject at most once per POINTER_INJECT_BACKOFF_SECS
                # instead of every poll. Prevents the keystroke hammer.
                now = time.time()
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
                ok = inject_via_tmux(session_name, summary)
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
                if not ok:
                    logger.error(
                        "Injection failed for %s; idle left set, will retry next poll",
                        session_name,
                    )

            inbox_pattern = f"{key_prefix()}:*:inbox"
            for inbox in r.scan_iter(match=inbox_pattern):
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
