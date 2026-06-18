#!/usr/bin/env python3
"""Claude Code SessionStart hook - surface orchestrator wake context."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hooks._shared import action_session_start, emit_claude_or_codex, get_redis_and_node, read_stdin_json


def main() -> None:
    read_stdin_json()
    r, node_id = get_redis_and_node()
    if not r or not node_id:
        print(json.dumps({}))
        return
    emit_claude_or_codex("SessionStart", action_session_start(r, node_id))


if __name__ == "__main__":
    main()
