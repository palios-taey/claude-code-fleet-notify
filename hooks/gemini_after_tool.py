#!/usr/bin/env python3
"""Gemini CLI AfterTool hook — stamp activity, drain inbox, return
inbox messages + pending OrchTasks via additionalContext.

Wired in ~/.gemini/settings.json under "hooks.AfterTool".
Gemini stdin includes: session_id, transcript_path, cwd, hook_event_name,
model, turn_id, tool_name, tool_use_id, tool_input, tool_response.

Stdout silence is mandatory — only the JSON envelope, no logging.
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shared import (
    read_stdin_json, get_redis_and_node, action_post_tool, emit_gemini,
)


def main() -> None:
    try:
        payload = read_stdin_json()
        tool_name = payload.get("tool_name", "")

        context = ""
        r, node_id = get_redis_and_node()
        if r is not None and node_id is not None:
            context = action_post_tool(r, node_id, tool_name)
        emit_gemini(context if context else None)
    except Exception:
        try:
            emit_gemini(None)
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
