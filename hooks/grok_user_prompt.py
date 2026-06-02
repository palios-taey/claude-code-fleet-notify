#!/usr/bin/env python3
"""Grok CLI UserPromptSubmit hook — clear idle flag.

Wired via ~/.grok/hooks/cf-notify.json.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shared import (
    read_stdin_json, get_redis_and_node, action_user_prompt, emit_claude_or_codex,
)


def _resolve_grok_node_id(data: dict) -> str | None:
    explicit = os.environ.get("TAEY_NODE_ID")
    if explicit:
        return explicit
    for raw in (
        data.get("workspaceRoot"),
        data.get("workspace_root"),
        data.get("cwd"),
        os.environ.get("GROK_WORKSPACE_ROOT"),
        os.getcwd(),
    ):
        if not raw:
            continue
        base = os.path.basename(os.path.normpath(str(raw)))
        if base.endswith("-grok"):
            return base
    return None


def _grok_redis_and_node(data: dict):
    node_id = _resolve_grok_node_id(data)
    if node_id:
        try:
            from identity import redis_connect
            r = redis_connect()
            r.ping()
            return r, node_id
        except Exception:
            pass
    return get_redis_and_node()


def main() -> None:
    data = read_stdin_json()

    r, node_id = _grok_redis_and_node(data)
    if r is None:
        emit_claude_or_codex("UserPromptSubmit", None)
        sys.exit(0)

    context = action_user_prompt(r, node_id)
    emit_claude_or_codex("UserPromptSubmit", context if context else None)
    sys.exit(0)


if __name__ == "__main__":
    main()
