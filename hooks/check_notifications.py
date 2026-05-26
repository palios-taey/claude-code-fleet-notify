#!/usr/bin/env python3
"""PostToolUse hook — drain queued notifications after every tool call.

Sources:
1. ``${NOTIFY_KEY_PREFIX:-taey}:{node_id}:inbox`` — inter-session messages
2. ``${NOTIFY_KEY_PREFIX:-taey}:{node_id}:notifications`` — worker / monitor notifications
3. ``${NOTIFY_KEY_PREFIX:-taey}:notify:{node_id}:orch`` — auxiliary notifications

Also clears ``tool_running`` so the idle daemon knows the session is between tool
calls. Delivery is via ``hookSpecificOutput.additionalContext``.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Load .env so OrchConfig picks up ORCH_NEO4J_URI
_env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.isfile(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                _key = _key.replace("export ", "").strip()
                os.environ.setdefault(_key, _val.strip())

from identity import detect_node_id, redis_connect
from notifications.inbox import drain_all, flatten_sources, format_notification_block, state_key

LOG_FILE = "/tmp/notify_hook_debug.log"


def log_debug(msg: str) -> None:
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[{datetime.datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


def main() -> None:
    log_debug("PostToolUse hook started")

    try:
        input_data = json.load(sys.stdin)
        tool_name = input_data.get("tool_name", "unknown")
        log_debug(f"Tool completed: {tool_name}")
    except (json.JSONDecodeError, EOFError) as e:
        log_debug(f"Input error: {e}")
        print(json.dumps({}))
        sys.exit(0)

    try:
        node_id = detect_node_id()
        r = redis_connect()
        r.ping()
        log_debug(f"Connected to Redis, node_id={node_id}")
    except Exception as e:
        log_debug(f"Redis connect failed: {e}")
        print(json.dumps({}))
        sys.exit(0)

    # Clear tool_running flag and stamp last activity.
    try:
        r.delete(state_key(node_id, "tool_running"))
        r.set(state_key(node_id, "last_activity"), str(time.time()))
    except Exception as e:
        log_debug(f"Activity clear error: {e}")

    messages = []
    try:
        drained = drain_all(r, node_id)
        messages = flatten_sources(drained)
        log_debug(f"Drained: {len(messages)} messages")
    except Exception as e:
        log_debug(f"Drain error: {e}\n{traceback.format_exc()}")

    if messages:
        context = format_notification_block(messages)
        log_debug(f"Injecting {len(messages)} messages ({len(context)} chars)")
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": context,
                    }
                }
            )
        )
    else:
        print(json.dumps({}))

    sys.exit(0)


if __name__ == "__main__":
    main()
