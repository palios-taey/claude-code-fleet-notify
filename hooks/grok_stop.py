#!/usr/bin/env python3
"""Grok CLI Stop hook — set idle=1 and notify supervisor on stop.

Wired via ~/.grok/hooks/cf-notify.json.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_IMPORT_ERROR = None
try:
    from _shared import (
        read_stdin_json, get_redis_and_node, action_stop, emit_claude_or_codex,
        fetch_stop_decision, _cache_stop_decision,
    )
except Exception as _exc:
    _IMPORT_ERROR = _exc
    read_stdin_json = get_redis_and_node = action_stop = emit_claude_or_codex = fetch_stop_decision = _cache_stop_decision = None


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


def _emit_fail_open() -> None:
    try:
        sys.stdin.read()
    except Exception:
        pass
    print(json.dumps({}))
    sys.exit(0)


def main() -> None:
    if _IMPORT_ERROR is not None:
        _emit_fail_open()
    data = read_stdin_json()
    stop_hook_active = bool(data.get("stop_hook_active", False))

    r, node_id = _grok_redis_and_node(data)
    if r is None:
        emit_claude_or_codex("Stop", None)
        sys.exit(0)

    decision = fetch_stop_decision(node_id, stop_hook_active=stop_hook_active)
    if decision:
        _cache_stop_decision(r, node_id, decision)
        if decision.get("block"):
            print(json.dumps({"decision": "block", "reason": decision.get("reason")}))
            sys.exit(0)

    action_stop(r, node_id)
    emit_claude_or_codex("Stop", None)
    sys.exit(0)


if __name__ == "__main__":
    main()
