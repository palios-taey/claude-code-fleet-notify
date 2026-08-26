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
from _shared import (
    _live_guard_unavailable_decision,
    get_redis_and_node,
    live_guard_mutation_intent,
)


_DEFECT_SIGNAL_TTL_SECONDS = 15 * 60


def _defect_class(reason: str) -> str:
    lower = reason.lower()
    for defect_class, marker in (
        ("registry_config_missing", "no registry path configured"),
        ("registry_absent", "registry file absent"),
        ("registry_unreadable", "registry file unreadable"),
        ("registry_invalid", "registry file"),
        ("command_parse", "unparseable shell command"),
        ("command_parse", "could not prove this call read-only"),
        ("wrapper_internal", "hook wrapper internal error"),
        ("guard_internal", "internal error"),
    ):
        if marker in lower:
            return defect_class
    return "unknown_guard_state"


def _route_defect_once(reason: str) -> None:
    if "LIVE-PATH GUARD DEFECT" not in reason:
        return
    r, node_id = get_redis_and_node()
    if r is None or node_id is None:
        log_debug("live-path-guard", "owner defect route unavailable: Redis/node identity unavailable")
        return
    from notifications.inbox import key_prefix, send
    from notifications.targets import resolve_supervisor

    owner = resolve_supervisor(r, node_id, prefix=key_prefix()) or node_id
    defect_class = _defect_class(reason)
    dedupe_key = f"{key_prefix()}:live-path-guard:defect:{node_id}:{defect_class}"
    if not r.set(dedupe_key, "1", nx=True, ex=_DEFECT_SIGNAL_TTL_SECONDS):
        return
    try:
        send(
            r,
            owner,
            (
                f"DEFECT: live-path guard is degraded on {node_id} "
                f"({defect_class}). Read-only calls continue; guarded mutating shell "
                "calls fail closed. Repair the committed registry/configuration before "
                "retrying a blocked mutation."
            ),
            msg_type="defect",
            from_node=node_id,
            priority="high",
            guard_failure_class=defect_class,
        )
    except Exception:
        r.delete(dedupe_key)
        raise


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
        defect = f"LIVE-PATH GUARD DEFECT: hook wrapper internal error: {exc}"
        try:
            intent = live_guard_mutation_intent(cwd, tool_name, tool_input)
        except Exception:
            shell_input = isinstance(tool_input, str) or (
                isinstance(tool_input, dict)
                and any(key in tool_input for key in ("command", "cmd", "shell_command"))
            )
            intent = "unknown" if shell_input or tool_name in {
                "Bash", "Shell", "run_shell_command"
            } else "read_only"
        allowed, reason = _live_guard_unavailable_decision(intent, defect)
        log_debug("live-path-guard", defect)

    if "LIVE-PATH GUARD DEFECT" in reason:
        try:
            _route_defect_once(reason)
        except Exception as exc:
            log_debug("live-path-guard", f"owner defect route failed: {exc}")
        if allowed:
            reason = ""

    if event_name == "BeforeTool":
        _emit_gemini(allowed, reason)
    else:
        _emit_claude_or_codex("PreToolUse", allowed, reason)


if __name__ == "__main__":
    main()
