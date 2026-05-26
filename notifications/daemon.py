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

import json

from notifications.inbox import (
    has_pending_messages,
    inbox_key,
    is_node_idle,
    key_prefix,
    notifications_key,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [notify-daemon] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 3
MAX_MESSAGE_LENGTH = 4000


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
            for session_name in get_local_tmux_sessions():
                node_id = session_name

                if not is_node_idle(r, node_id):
                    continue
                if not has_pending_messages(r, node_id):
                    continue

                # POINTER ONLY — peek inbox, build summary, inject summary into tmux.
                # Full message bodies stay in Redis. Recipient reads with hook on next tool call.
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
                if not inject_via_tmux(session_name, summary):
                    logger.error(
                        "Injection failed for %s; idle left set, will retry next poll",
                        session_name,
                    )

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
