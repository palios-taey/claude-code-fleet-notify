#!/usr/bin/env python3
"""Gemini CLI BeforeAgent hook — clear idle flag (the user/agent is active).

Wired in ~/.gemini/settings.json under "hooks.BeforeAgent".
Semantically equivalent to Claude/Codex UserPromptSubmit — fires when
the agent begins processing a turn, indicating the session is active.

Stdout silence is mandatory — only the JSON envelope, no logging.
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shared import (
    read_stdin_json, get_redis_and_node, action_user_prompt, emit_gemini,
)


def main() -> None:
    read_stdin_json()  # consume stdin

    r, node_id = get_redis_and_node()
    if r is None:
        emit_gemini(None)
        sys.exit(0)

    context = action_user_prompt(r, node_id)
    emit_gemini(context if context else None)
    sys.exit(0)


if __name__ == "__main__":
    main()
