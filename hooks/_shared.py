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


def _resolve_supervisor(r, node_id: str) -> Optional[str]:
    """Resolve who supervises this node, if anyone.

    Resolution order:
    1. Explicit override key ``taey:<node>:parent`` — set by orchestrators
       that need multi-level supervisor trees (a worker that is itself a
       supervisor for its own children).
    2. Suffix-strip rule for CLI peers: ``<name>-codex`` / ``<name>-gemini``
       / ``<name>-grok`` strip to ``<name>``, which is then treated as the
       supervisor session.
    3. Top-level sessions (no recognized suffix and no explicit override)
       return ``None`` — they have no supervisor to notify.

    The explicit key wins because the suffix rule can't distinguish a
    second-level worker (e.g., ``treasurer-codex-research``) from a
    first-level one.
    """
    try:
        from notifications.inbox import state_key

        explicit = r.get(state_key(node_id, "parent"))
        if explicit:
            return explicit
    except Exception:
        pass

    for suffix in ("-codex", "-gemini", "-grok"):
        if node_id.endswith(suffix):
            return node_id[: -len(suffix)]
    return None


_VALID_OUTCOMES = ("done", "error", "interrupted", "unknown")


def _current_task_summary(r, node_id: str):
    """Build a short summary of the worker's just-completed task, if any.

    Reads two optional keys that the dispatcher / worker maintain:

    - ``taey:<node>:current_task`` — JSON {task_id, description,
      supervisor, started_at} written by ``dispatch()`` when work is
      assigned.
    - ``taey:<node>:last_outcome`` — JSON {outcome, details} OR a raw
      string (treated as ``unknown`` + details). The worker may set this
      via ``orchestrator.record_outcome()`` before stopping. Absent means
      ``unknown`` (worker stopped without explicit signal — could be
      clean finish, could be error-restart).

    Returns ``(summary_text, outcome)`` where ``outcome`` is one of
    ``done|error|interrupted|unknown`` and is load-bearing for the
    caller's decision to clear current_task (only clear on ``done``).

    Returns ``("", None)`` if there is no current task at all.

    Gaia (Phase A consultation 2026-05-26): the outcome enum is required
    because the Stop signal alone overloads two opposite meanings — clean
    finish AND error-then-restart — and a supervisor that infers
    completion from idle silently mishandles half the failure modes.
    """
    try:
        from notifications.inbox import state_key

        raw = r.get(state_key(node_id, "current_task"))
        if not raw:
            return "", None
        try:
            task = json.loads(raw)
        except Exception:
            task = {"description": raw[:80]}

        task_id = task.get("task_id", "?")
        desc = (task.get("description") or "")[:120]
        started_at = task.get("started_at")

        # last_outcome: structured JSON preferred; raw string falls back
        # to outcome=unknown + details=raw.
        outcome = "unknown"
        details = ""
        last_outcome_raw = r.get(state_key(node_id, "last_outcome"))
        if last_outcome_raw:
            try:
                parsed = json.loads(last_outcome_raw)
                outcome = parsed.get("outcome", "unknown")
                if outcome not in _VALID_OUTCOMES:
                    outcome = "unknown"
                details = (parsed.get("details") or "")[:200]
            except (json.JSONDecodeError, AttributeError):
                details = last_outcome_raw[:200]

        bits = [f"outcome={outcome}", f"task={task_id}"]
        if desc:
            bits.append(f'"{desc}"')
        if details:
            bits.append(f"details={details}")
        if started_at:
            try:
                elapsed = int(time.time() - float(started_at))
                bits.append(f"duration={elapsed}s")
            except Exception:
                pass
        return "; ".join(bits), outcome
    except Exception as e:
        log_debug(node_id, f"current_task summary error: {e}")
        return "", None


def _notify_supervisor_of_stop(r, node_id: str, supervisor: str) -> None:
    """Push a peer_idle message to the supervisor's inbox when this worker
    stops. Body includes the just-completed task summary + outcome enum,
    so the supervisor sees the result inline without context-switching to
    the worker pane.

    Dedup: 60s TTL on ``taey:peer-idle-notified:<node>`` prevents back-to-
    back Stops on the same worker from spamming the supervisor.

    Persistence rule (Gaia, Phase A consultation 2026-05-26): clear
    current_task ONLY when the outcome is explicitly ``done``. For
    ``error``, ``interrupted``, or ``unknown`` outcomes the current_task
    persists — it is the signal "previous dispatch did not complete
    cleanly" that the next dispatch attempt MUST observe. Without this,
    error-then-restart cycles silently look identical to clean completions.
    """
    try:
        from notifications.inbox import inbox_key, state_key

        dedup = f"taey:peer-idle-notified:{node_id}"
        if r.exists(dedup):
            return

        summary, outcome = _current_task_summary(r, node_id)
        if summary:
            body = f"{node_id} stopped — {summary}"
            priority = "high" if outcome in ("error", "interrupted") else "normal"
        else:
            body = f"{node_id} stopped — no current task recorded"
            priority = "low"

        msg = json.dumps({
            "from": node_id,
            "type": "peer_idle",
            "body": body,
            "outcome": outcome,  # explicit enum field for programmatic readers
            "priority": priority,
            "msg_id": f"peer-idle-{node_id}-{int(time.time())}",
            "timestamp": time.time(),
        })
        r.lpush(inbox_key(supervisor), msg)
        r.set(dedup, "1", ex=60)

        # Clear keys ONLY on confirmed completion. Anything else (error,
        # interrupted, unknown, or no task at all) leaves current_task in
        # place so the supervisor's next dispatch sees the unresolved state.
        if outcome == "done":
            r.delete(state_key(node_id, "current_task"))
            r.delete(state_key(node_id, "last_outcome"))

        log_debug(node_id,
                  f"STOP: notified supervisor={supervisor} outcome={outcome} body=\"{body}\"")
    except Exception as e:
        log_debug(node_id, f"notify_supervisor error: {e}")


def action_stop(r, node_id: str) -> None:
    """Stop / AfterAgent: set idle=1 with no TTL (stopped means stopped until
    UserPromptSubmit clears it). Clear tool_running. Stamp last_activity.
    Notify supervisor (if any) with completed-task summary.

    NOTE: this is the ONLY place idle gets set. Per NOTIFICATION_PROTOCOL.md.

    Universal Stop+notify primitive (v0.2.0): the Stop hook is the canonical
    notifier for worker→supervisor signaling. Don't trust workers to call
    taey-notify manually — every Stop fires the parent-notify automatically,
    with task content from ``taey:<node>:current_task`` (set by the
    dispatcher) and optional outcome from ``taey:<node>:last_outcome``
    (worker may set this before stopping). Supervisor receives outcome
    inline.
    """
    try:
        from notifications.inbox import state_key

        r.set(state_key(node_id, "idle"), "1")
        r.delete(state_key(node_id, "tool_running"))
        r.set(state_key(node_id, "last_activity"), str(time.time()))
        log_debug(node_id, "STOP: idle=1")

        supervisor = _resolve_supervisor(r, node_id)
        if supervisor:
            _notify_supervisor_of_stop(r, node_id, supervisor)
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
