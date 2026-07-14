#!/usr/bin/env python3
"""Codex CLI Stop hook — set idle=1 (no TTL).

Wired in ~/.codex/config.toml under [[hooks.Stop]].
Codex stdin includes: session_id, transcript_path, cwd, hook_event_name,
model, turn_id.

Stop is one valid setter for idle=1; SessionStart and the daemon's narrow
at-rest reconciler can also set it. Per NOTIFICATION_PROTOCOL.md the
notification daemon will pointer-inject pending inbox messages via tmux once
it sees idle=1.
"""
from __future__ import annotations
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_IMPORT_ERROR = None
try:
    from _shared import (
        read_stdin_json, get_redis_and_node, action_stop, action_stop_idle, emit_claude_or_codex,
        fetch_stop_decision, _cache_stop_decision,
    )
except Exception as _exc:
    _IMPORT_ERROR = _exc
    read_stdin_json = get_redis_and_node = action_stop = action_stop_idle = emit_claude_or_codex = fetch_stop_decision = _cache_stop_decision = None


def _emit_fail_open() -> None:
    try:
        sys.stdin.read()
    except Exception:
        pass
    print(json.dumps({}))
    sys.exit(0)


def main() -> None:
    if _IMPORT_ERROR is not None:
        _emit_fail_open()
    data = read_stdin_json()
    stop_hook_active = bool(data.get("stop_hook_active", False))

    r, node_id = get_redis_and_node()
    if r is None:
        emit_claude_or_codex("Stop", None)
        sys.exit(0)

    decision = fetch_stop_decision(node_id, stop_hook_active=stop_hook_active)
    if decision:
        _cache_stop_decision(r, node_id, decision)
        if decision.get("block"):
            action_stop_idle(r, node_id)
            print(json.dumps({"decision": "block", "reason": decision.get("reason")}))
            sys.exit(0)

    action_stop(r, node_id)
    emit_claude_or_codex("Stop", None)
    sys.exit(0)


if __name__ == "__main__":
    main()
