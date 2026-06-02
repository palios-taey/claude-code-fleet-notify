#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hooks._shared import action_post_tool, emit_claude_or_codex, get_redis_and_node, read_stdin_json


def main() -> None:
    payload = read_stdin_json()
    tool_name = payload.get("tool_name", "unknown")
    r, node_id = get_redis_and_node()
    if not r or not node_id:
        print(json.dumps({}))
        return
    emit_claude_or_codex("PostToolUse", action_post_tool(r, node_id, tool_name=tool_name))


if __name__ == "__main__":
    main()
