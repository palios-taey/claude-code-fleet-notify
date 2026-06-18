#!/usr/bin/env python3
"""Codex CLI PostToolUse hook — stamp activity, drain inbox, return
inbox messages + pending OrchTasks via additionalContext.

Wired in ~/.codex/config.toml under [[hooks.PostToolUse]].
Codex stdin includes: session_id, transcript_path, cwd, hook_event_name,
model, turn_id, tool_name, tool_use_id, tool_input, tool_response.
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shared import (
    read_stdin_json, get_redis_and_node, action_post_tool, emit_claude_or_codex,
)


def main() -> None:
    payload = read_stdin_json()
    tool_name = payload.get("tool_name", "")

    r, node_id = get_redis_and_node()
    if r is None:
        emit_claude_or_codex("PostToolUse", None)
        sys.exit(0)

    context = action_post_tool(r, node_id, tool_name)
    emit_claude_or_codex("PostToolUse", context if context else None)
    sys.exit(0)


if __name__ == "__main__":
    main()
