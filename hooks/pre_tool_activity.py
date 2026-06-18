#!/usr/bin/env python3
"""
PreToolUse hook — mark node activity.

Daemon injection is gated only by idle=1; this hook only stamps activity
markers used by handoff activation.

Must be fast — adds ~50ms to every tool call.
"""
import json
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from identity import detect_node_id, redis_connect
from notifications.inbox import state_key


def main():
    try:
        sys.stdin.read()
    except Exception:
        pass

    try:
        r = redis_connect()
        node_id = detect_node_id()
        now = str(time.time())
        r.set(state_key(node_id, "last_activity"), now)
        r.set(state_key(node_id, "last_tool_activity"), now)
        # NOTE: Do NOT delete idle here — only UserPromptSubmit clears idle
    except Exception:
        pass

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "Activity tracked",
        }
    }))


if __name__ == "__main__":
    main()
