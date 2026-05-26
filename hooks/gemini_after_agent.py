#!/usr/bin/env python3
"""Gemini CLI AfterAgent hook — set idle=1 (no TTL).

Wired in ~/.gemini/settings.json under "hooks.AfterAgent".
Semantically equivalent to Claude/Codex Stop — fires when the agent
finishes processing a turn. Per NOTIFICATION_PROTOCOL.md the notification
daemon will pointer-inject pending inbox messages via tmux once it sees
idle=1.

Stdout silence is mandatory — only the JSON envelope, no logging.
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shared import (
    read_stdin_json, get_redis_and_node, action_stop, emit_gemini,
)


def main() -> None:
    read_stdin_json()  # consume stdin even if we don't use fields

    r, node_id = get_redis_and_node()
    if r is None:
        emit_gemini(None)
        sys.exit(0)

    action_stop(r, node_id)
    emit_gemini(None)
    sys.exit(0)


if __name__ == "__main__":
    main()
