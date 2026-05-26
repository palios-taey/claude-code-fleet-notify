#!/usr/bin/env python3
"""
Stop hook — Claude stopped responding. Set idle=1.

This is the ONLY way idle gets set. UserPromptSubmit is the ONLY way
it gets cleared. The notification router delivers via tmux ONLY when
idle=1.

NO SILENT FAILURES. If Redis fails, log to stderr and a file.
"""
import json
import sys
import os
import time
import traceback

# DIAGNOSTIC (2026-05-18): invocation marker — fires before any other logic
# so we can distinguish "hook never invoked by Claude Code" from "hook ran
# but crashed before logging". Records every invocation with PID/PPID
# regardless of session, for cross-session comparison.
try:
    with open('/tmp/stop_hook_invoked.log', 'a') as _f:
        _f.write(f"{time.strftime('%H:%M:%S')} PID={os.getpid()} PPID={os.getppid()}\n")
except Exception:
    pass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from notifications.inbox import inbox_key, notifications_key, state_key

LOG_FILE = "/tmp/stop_hook_debug.log"


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, file=sys.stderr)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main():
    try:
        sys.stdin.read()
    except Exception:
        pass

    try:
        from identity import detect_node_id, redis_connect
        node_id = detect_node_id()
        log(f"Stop hook fired for node={node_id}")

        r = redis_connect()
        r.ping()

        r.set(state_key(node_id, "idle"), "1")  # no TTL — stopped means stopped until UserPromptSubmit clears it
        r.delete(state_key(node_id, "tool_running"))
        r.set(state_key(node_id, "last_activity"), str(time.time()))

        # Check pending messages
        inbox = r.llen(inbox_key(node_id)) or 0
        notif = r.llen(notifications_key(node_id)) or 0
        if inbox + notif > 0:
            log(f"PENDING: {inbox} inbox + {notif} notifications waiting for {node_id}")

        log(f"Set idle=1 for {node_id}")
    except Exception as e:
        log(f"STOP HOOK FAILED: {e}\n{traceback.format_exc()}")

    print(json.dumps({}))


if __name__ == "__main__":
    main()
