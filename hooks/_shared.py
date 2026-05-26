"""Shared helpers for fleet hook scripts (Claude / Codex / Gemini).

The four hook events have the same Redis state-machine semantics regardless of
which CLI fires them; only stdin schema and stdout envelope differ. This module
factors out the common parts so per-CLI entry-point scripts stay thin (~30
lines each).

Hooks coverage:
    Claude:   PreToolUse / PostToolUse / Stop / UserPromptSubmit
    Codex:    PreToolUse / PostToolUse / Stop / UserPromptSubmit (same names)
    Gemini:   BeforeTool / AfterTool / AfterAgent / BeforeAgent

Output envelope differs slightly:
    Claude / Codex:  {"hookSpecificOutput": {"hookEventName": "...", "additionalContext": "..."}}
    Gemini:          {"hookSpecificOutput": {"additionalContext": "..."}}

The functions below DO the Redis work + return string context; the entry-point
scripts wrap them in the right envelope.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
import traceback
from typing import Optional

# ---- env + path setup ----

# Add the package root to sys.path so we can import identity and notifications.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Load .env so OrchConfig picks up ORCH_NEO4J_URI etc.
_env_path = os.path.join(_REPO_ROOT, ".env")
if os.path.isfile(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                _key = _key.replace("export ", "").strip()
                os.environ.setdefault(_key, _val.strip())


def log_path_for(node_id: str) -> str:
    """Per-node hook log file."""
    return f"/tmp/{node_id}-hooks.log"


def log_debug(node_id: str, msg: str) -> None:
    try:
        with open(log_path_for(node_id), "a") as f:
            f.write(f"[{datetime.datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


def read_stdin_json() -> dict:
    """Read JSON envelope from stdin, return empty dict on parse failure
    (hooks should never block their CLI on stdin issues)."""
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        return {}


def get_redis_and_node():
    """Connect to Redis + detect this session's node_id.
    Returns (redis_client, node_id) or (None, None) on failure."""
    try:
        from identity import detect_node_id, redis_connect
        node_id = detect_node_id()
        r = redis_connect()
        r.ping()
        return r, node_id
    except Exception as e:
        # Don't crash the host CLI if Redis is unreachable
        try:
            log_debug("unknown", f"Redis/identity failure: {e}")
        except Exception:
            pass
        return None, None


# ---- the four state-machine actions ----

def action_pre_tool(r, node_id: str, tool_name: str = "") -> None:
    """PreToolUse / BeforeTool: set tool_running flag with 60s TTL safety net,
    stamp last_activity. Same semantics as Claude's pre_tool_activity.py."""
    try:
        from notifications.inbox import state_key

        r.set(state_key(node_id, "tool_running"), "1", ex=60)
        r.set(state_key(node_id, "last_activity"), str(time.time()))
        log_debug(node_id, f"PRE-TOOL: tool={tool_name}")
    except Exception as e:
        log_debug(node_id, f"action_pre_tool error: {e}")


def action_post_tool(r, node_id: str, tool_name: str = "") -> str:
    """PostToolUse / AfterTool: clear tool_running, drain inbox + notifications +
    orch streams, return formatted context string for additionalContext.
    Empty string means nothing to surface."""
    try:
        from notifications.inbox import state_key

        r.delete(state_key(node_id, "tool_running"))
        r.set(state_key(node_id, "last_activity"), str(time.time()))
    except Exception as e:
        log_debug(node_id, f"post_tool clear error: {e}")

    # Drain message queues
    messages = []
    try:
        from notifications.inbox import drain_all, flatten_sources
        drained = drain_all(r, node_id)
        messages = flatten_sources(drained)
        log_debug(node_id, f"POST-TOOL: drained {len(messages)} msgs (tool={tool_name})")
    except Exception as e:
        log_debug(node_id, f"drain error: {e}\n{traceback.format_exc()}")


    if not messages:
        return ""

    try:
        from notifications.inbox import format_notification_block
        return format_notification_block(messages)
    except Exception as e:
        log_debug(node_id, f"format error: {e}")
        # Fallback: minimal text
        body = "\n".join(f"[{m.get('type','msg')} from {m.get('from','?')}]: {m.get('body','')[:200]}"
                          for m in messages)
        return body


def action_stop(r, node_id: str) -> None:
    """Stop / AfterAgent: set idle=1 with no TTL (stopped means stopped until
    UserPromptSubmit clears it). Clear tool_running. Stamp last_activity.
    NOTE: this is the ONLY place idle gets set. Per NOTIFICATION_PROTOCOL.md.

    """
    try:
        from notifications.inbox import state_key

        r.set(state_key(node_id, "idle"), "1")
        r.delete(state_key(node_id, "tool_running"))
        r.set(state_key(node_id, "last_activity"), str(time.time()))
        log_debug(node_id, "STOP: idle=1")
    except Exception as e:
        log_debug(node_id, f"action_stop error: {e}")


def action_user_prompt(r, node_id: str) -> str:
    """UserPromptSubmit / BeforeAgent: clear idle flag (the user is back),
    stamp last_activity, drain inbox so daemon-injected pointers don't
    redeliver if the recipient responds with text only.

    Returns a formatted notification block as additionalContext (same
    format PostToolUse uses). Drained messages MUST be surfaced here so
    the recipient sees them on this turn even if no tool call fires.
    Per task-4b841b72: text-only responses without this drain caused
    the daemon-redelivery spam loop.

    NOTE: this is the ONLY place idle gets cleared by hooks."""
    try:
        from notifications.inbox import state_key

        r.delete(state_key(node_id, "idle"))
        r.set(state_key(node_id, "last_activity"), str(time.time()))
    except Exception as e:
        log_debug(node_id, f"action_user_prompt error: {e}")

    messages = []
    try:
        from notifications.inbox import drain_all, flatten_sources
        drained = drain_all(r, node_id)
        messages = flatten_sources(drained)
        log_debug(node_id, f"USER-PROMPT: idle cleared, drained {len(messages)} msgs")
    except Exception as e:
        log_debug(node_id, f"USER-PROMPT drain error: {e}")

    if not messages:
        return ""

    try:
        from notifications.inbox import format_notification_block
        return format_notification_block(messages, task_summary="")
    except Exception as e:
        log_debug(node_id, f"USER-PROMPT format error: {e}")
        return "\n".join(
            f"[{m.get('type','msg')} from {m.get('from','?')}]: {m.get('body','')[:200]}"
            for m in messages
        )


# ---- output envelope helpers ----

def emit_claude_or_codex(event_name: str, additional_context: Optional[str] = None) -> None:
    """Output envelope for Claude Code and Codex CLI hooks. Both expect
    {"hookSpecificOutput": {"hookEventName": "...", "additionalContext": "..."}}."""
    if additional_context:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": additional_context,
            }
        }))
    else:
        print(json.dumps({}))


def emit_gemini(additional_context: Optional[str] = None) -> None:
    """Output envelope for Gemini CLI hooks. Gemini doesn't require
    hookEventName in the response. Stdout silence is mandatory — only
    the JSON, no logging."""
    if additional_context:
        print(json.dumps({
            "hookSpecificOutput": {
                "additionalContext": additional_context,
            }
        }))
    else:
        print(json.dumps({}))
