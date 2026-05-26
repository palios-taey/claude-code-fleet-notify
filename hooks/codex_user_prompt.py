#!/usr/bin/env python3
"""Codex CLI UserPromptSubmit hook — clear idle flag.

Wired in ~/.codex/config.toml under [[hooks.UserPromptSubmit]].
Codex stdin includes: session_id, transcript_path, cwd, hook_event_name,
model, turn_id, prompt.
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shared import (
    read_stdin_json, get_redis_and_node, action_user_prompt, emit_claude_or_codex,
)


def main() -> None:
    read_stdin_json()  # consume stdin

    r, node_id = get_redis_and_node()
    if r is None:
        emit_claude_or_codex("UserPromptSubmit", None)
        sys.exit(0)

    context = action_user_prompt(r, node_id)
    emit_claude_or_codex("UserPromptSubmit", context if context else None)
    sys.exit(0)


if __name__ == "__main__":
    main()
