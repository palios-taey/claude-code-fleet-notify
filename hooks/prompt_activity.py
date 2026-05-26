#!/usr/bin/env python3
"""
UserPromptSubmit hook — Claude received input. Clear idle=1.

This fires when:
- User types a prompt
- tmux injection delivers a message (which IS a prompt)

Clears idle so the router stops injecting while Claude works.
NO SILENT FAILURES.
"""
import json
import sys
import os
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from notifications.inbox import state_key

LOG_FILE = "/tmp/prompt_hook_debug.log"


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

    additional_context = ""
    try:
        from identity import detect_node_id, redis_connect
        node_id = detect_node_id()

        r = redis_connect()
        r.delete(state_key(node_id, "idle"))
        r.set(state_key(node_id, "last_activity"), str(time.time()))

        # Drain inbox so daemon-injected pointers don't redeliver if the
        # recipient responds with text only (no PostToolUse drain). Surface
        # drained messages as additionalContext so they're still visible
        # on this turn. Per task-4b841b72.
        try:
            from notifications.inbox import drain_all, flatten_sources, format_notification_block
            drained = drain_all(r, node_id)
            messages = flatten_sources(drained)
            log(f"Cleared idle for {node_id}, drained {len(messages)} msgs")
            if messages:
                additional_context = format_notification_block(messages, task_summary="")
        except Exception as e:
            log(f"PROMPT HOOK drain failed: {e}")

    except Exception as e:
        log(f"PROMPT HOOK FAILED: {e}\n{traceback.format_exc()}")

    if additional_context:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": additional_context,
            }
        }))
    else:
        print(json.dumps({}))


if __name__ == "__main__":
    main()
