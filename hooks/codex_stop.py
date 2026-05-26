#!/usr/bin/env python3
"""Codex CLI Stop hook — set idle=1 (no TTL).

Wired in ~/.codex/config.toml under [[hooks.Stop]].
Codex stdin includes: session_id, transcript_path, cwd, hook_event_name,
model, turn_id.

This is the ONLY place idle gets set. Per NOTIFICATION_PROTOCOL.md the
notification daemon will pointer-inject pending inbox messages via tmux
once it sees idle=1.
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shared import (
    read_stdin_json, get_redis_and_node, action_stop, emit_claude_or_codex,
)


def main() -> None:
    read_stdin_json()  # consume stdin even if we don't use fields

    r, node_id = get_redis_and_node()
    if r is None:
        emit_claude_or_codex("Stop", None)
        sys.exit(0)

    action_stop(r, node_id)
    emit_claude_or_codex("Stop", None)
    sys.exit(0)


if __name__ == "__main__":
    main()
