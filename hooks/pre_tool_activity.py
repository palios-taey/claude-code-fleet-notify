#!/usr/bin/env python3
"""
PreToolUse hook — mark node as active (running a tool).

Sets tool_running flag so daemon doesn't inject mid-tool.
The Stop hook and PostToolUse hook clear this.

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
        r.set(state_key(node_id, "tool_running"), "1", ex=60)  # 60s TTL, not 300
        r.set(state_key(node_id, "last_activity"), str(time.time()))
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
