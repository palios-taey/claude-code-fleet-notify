#!/usr/bin/env python3
"""Claude Code Stop hook — set idle=1 + notify supervisor on stop.

This is the ONLY place idle gets set. UserPromptSubmit is the ONLY way
it gets cleared. The notification router delivers via tmux ONLY when
idle=1.

Universal Stop+notify primitive (v0.2.0): if this session has a
supervisor (resolved via taey:<node>:parent override OR the
<name>-codex/<name>-gemini/<name>-grok suffix-strip rule), the supervisor
receives a peer_idle message including the just-completed task summary.
Workers do not need to call taey-notify manually on completion — the
Stop hook is the canonical notifier across all supported CLIs.

NO SILENT FAILURES. If Redis fails, log to stderr and a file.
"""
import json
import os
import sys
import time

# DIAGNOSTIC (2026-05-18): invocation marker — fires before any other logic
# so we can distinguish "hook never invoked by Claude Code" from "hook ran
# but crashed before logging". Records every invocation with PID/PPID
# regardless of session, for cross-session comparison.
try:
    with open('/tmp/stop_hook_invoked.log', 'a') as _f:
        _f.write(f"{time.strftime('%H:%M:%S')} PID={os.getpid()} PPID={os.getppid()}\n")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_IMPORT_ERROR = None
try:
    from _shared import (
        read_stdin_json, get_redis_and_node, action_stop, emit_claude_or_codex,
        fetch_stop_decision, _cache_stop_decision,
    )
except Exception as _exc:
    _IMPORT_ERROR = _exc
    read_stdin_json = get_redis_and_node = action_stop = emit_claude_or_codex = fetch_stop_decision = _cache_stop_decision = None


def _emit_fail_open() -> None:
    try:
        sys.stdin.read()
    except Exception:
        pass
    print(json.dumps({}))
    sys.exit(0)


def main():
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
            print(json.dumps({"decision": "block", "reason": decision.get("reason")}))
            sys.exit(0)

    action_stop(r, node_id)
    emit_claude_or_codex("Stop", None)
    sys.exit(0)


if __name__ == "__main__":
    main()
