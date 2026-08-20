#!/usr/bin/env python3
"""Pre-tool live-path guard for parent fleet sessions.

This hook is deliberately separate from the activity hook: activity tracking
must stay cheap and reliable, while the live-path guard owns the blocking
decision for destructive operations against registered live checkouts/DBs.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shared import live_guard_decision, log_debug, read_stdin_json


def _emit_claude_or_codex(event_name: str, allowed: bool, reason: str) -> None:
    if allowed:
        if reason:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": reason,
                }
            }))
        else:
            print(json.dumps({}))
        return

    payload = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload))


def _emit_gemini(allowed: bool, reason: str) -> None:
    if allowed:
        print(json.dumps({"systemMessage": reason} if reason else {}))
        return
    print(json.dumps({"decision": "block", "reason": reason}))


def main() -> None:
    payload = read_stdin_json()
    event_name = str(payload.get("hook_event_name") or payload.get("hookEventName") or "PreToolUse")
    tool_name = str(payload.get("tool_name") or payload.get("toolName") or "")
    cwd = str(payload.get("cwd") or os.getcwd())
    tool_input = payload.get("tool_input", payload.get("toolInput", {}))

    try:
        allowed, reason = live_guard_decision(cwd, tool_name, tool_input)
    except Exception as exc:
        reason = f"LIVE-PATH GUARD WARNING: hook wrapper fail-open: {exc}"
        log_debug("live-path-guard", reason)
        allowed = True

    if event_name == "BeforeTool":
        _emit_gemini(allowed, reason)
    else:
        _emit_claude_or_codex("PreToolUse", allowed, reason)


if __name__ == "__main__":
    main()
