#!/usr/bin/env python3
"""Codex CLI SessionStart hook - surface orchestrator wake context."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shared import (
    action_session_start,
    emit_claude_or_codex,
    get_redis_and_node,
    read_stdin_json,
)


def main() -> None:
    read_stdin_json()

    r, node_id = get_redis_and_node()
    if r is None:
        emit_claude_or_codex("SessionStart", None)
        sys.exit(0)

    emit_claude_or_codex("SessionStart", action_session_start(r, node_id))
    sys.exit(0)


if __name__ == "__main__":
    main()
