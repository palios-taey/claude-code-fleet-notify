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
import dataclasses
import importlib
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from typing import Any, Optional

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

_ORCH_REPO_ROOT = os.environ.get("ORCH_REPO_ROOT", "/home/mira/claude-code-fleet-orchestrator")
if _ORCH_REPO_ROOT not in sys.path:
    sys.path.insert(0, _ORCH_REPO_ROOT)

from notifications.inbox import (
    WAKE_ALLOW_STOP,
    WAKE_ENGINE_ERROR,
    WAKE_REASON_REQUIRED,
    WAKE_WITH_QUEUE,
)

_ORCH_API_BASE = os.environ.get("ORCH_API_BASE", "http://127.0.0.1:5002")
_ENGINE_ERROR_WINDOW_SECS = 60
_ENGINE_ERROR_THRESHOLD = 3
_DEFAULT_HEARTBEAT_SECS = 300


@dataclasses.dataclass
class StopDecision:
    wake_type: str
    body: str = ""
    project_id: Optional[str] = None
    phase_id: Optional[str] = None
    task_id: Optional[str] = None
    task_title_short: Optional[str] = None
    task_priority: Optional[int] = None
    resume_context_pointer: Optional[str] = None
    available_conditions: Optional[list[dict[str, Any]]] = None
    next_action: Optional[str] = None
    error_key: Optional[str] = None
    audit_events: list[str] = dataclasses.field(default_factory=list)


def log_path_for(node_id: str) -> str:
    """Per-node hook log file."""
    return f"/tmp/{node_id}-hooks.log"


def log_debug(node_id: str, msg: str) -> None:
    try:
        with open(log_path_for(node_id), "a") as f:
            f.write(f"[{datetime.datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


def _api_json(path: str, method: str = "GET", payload: Optional[dict] = None, timeout: int = 5) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{_ORCH_API_BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _redis_get_many(r, keys: list[str]) -> list[Optional[str]]:
    if hasattr(r, "mget"):
        return list(r.mget(keys))
    return [r.get(key) for key in keys]


def _load_orch_modules():
    orch_schema = importlib.import_module("lib.orch_schema")
    orch_config = importlib.import_module("lib.config")
    return orch_schema, orch_config


def _get_session_supervised_projects(session_id: str) -> list[dict]:
    orch_schema, _ = _load_orch_modules()
    return orch_schema.get_session_supervised_projects(session_id)


def _get_session_next_ready(session_id: str, project_id: Optional[str] = None) -> Optional[dict]:
    orch_schema, _ = _load_orch_modules()
    return orch_schema.get_session_next_ready(session_id, project_id=project_id)


def _active_conditions(project: dict) -> list[dict]:
    return [cond for cond in list(project.get("user_stop_conditions") or []) if not cond.get("deprecated_at")]


def _project_has_valid_stop(project: dict) -> bool:
    return bool(project.get("stop_reason_current")) and not bool(project.get("stop_reason_orphaned"))


def _fetch_in_progress_projects(supervisor: str) -> list[dict]:
    _, orch_config = _load_orch_modules()
    cfg = orch_config.OrchConfig()
    driver = orch_config.get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run(
            """
            MATCH (p:OrchProject)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE coalesce(p.supervisor, '') = $supervisor
              AND coalesce(p.status, 'active') = 'in_progress'
              AND coalesce(t.status, '') = 'in_progress'
            RETURN p.id AS project_id,
                   t.id AS task_id,
                   t.owner AS owner,
                   coalesce(t.heartbeat_exempt_secs, 0) AS heartbeat_exempt_secs
            ORDER BY coalesce(p.priority, 999999999) ASC, t.created_at ASC
            """
            , supervisor=supervisor
        )
        return [dict(row) for row in result]


def _mark_project_active(project_id: str) -> None:
    _, orch_config = _load_orch_modules()
    cfg = orch_config.OrchConfig()
    driver = orch_config.get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        session.run(
            """
            MATCH (p:OrchProject {id: $project_id})
            SET p.status = 'active',
                p.updated_at = datetime()
            """,
            project_id=project_id,
        )


def _expire_stale_in_progress_projects(r, supervisor: str) -> list[str]:
    from notifications.inbox import state_key

    rows = _fetch_in_progress_projects(supervisor)
    if not rows:
        return []
    keys = [state_key(row.get("owner") or "", "last_activity") for row in rows]
    values = _redis_get_many(r, keys)
    now = time.time()
    audit_events = []
    for row, raw_last_activity in zip(rows, values):
        owner = row.get("owner") or ""
        if not owner:
            continue
        try:
            last_activity = float(raw_last_activity) if raw_last_activity is not None else 0.0
        except (TypeError, ValueError):
            last_activity = 0.0
        threshold = max(_DEFAULT_HEARTBEAT_SECS, int(row.get("heartbeat_exempt_secs") or 0))
        stalled_for = int(now - last_activity) if last_activity else threshold + 1
        if stalled_for <= threshold:
            continue
        _mark_project_active(str(row["project_id"]))
        audit_events.append(
            f"in_progress_expired: {row['project_id']}, owner={owner}, stalled_for={stalled_for}s"
        )
    return audit_events


def _session_pause_active(r, supervisor: str) -> bool:
    from notifications.inbox import state_key

    try:
        return bool(r.exists(state_key(supervisor, "pause")))
    except Exception as exc:
        log_debug(supervisor, f"pause check error: {exc}")
        return False


def _open_orchestrator_bug_lock(reason: str, owner: str) -> None:
    support_root = os.environ.get("CF_SUPPORT_REPO_ROOT", "/home/mira/claude-code-fleet-support")
    code = (
        "import sys; "
        f"sys.path.insert(0, {support_root!r}); "
        "from lib.buglock import open_bug_lock; "
        f"open_bug_lock('claude-code-fleet-orchestrator', {reason!r}, {owner!r})"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=10)


def _record_engine_error(r, supervisor: str, exc: Exception) -> tuple[str, int]:
    timestamp = time.time()
    event_key = f"taey:engine_error:{supervisor}:{int(timestamp)}"
    payload = {
        "session": supervisor,
        "error": repr(exc),
        "traceback": traceback.format_exc(),
        "ts": timestamp,
    }
    r.set(event_key, json.dumps(payload), ex=3600)
    recent_key = f"taey:engine_error:{supervisor}:recent"
    try:
        recent = json.loads(r.get(recent_key) or "[]")
    except Exception:
        recent = []
    recent = [float(item) for item in recent if timestamp - float(item) <= _ENGINE_ERROR_WINDOW_SECS]
    recent.append(timestamp)
    r.set(recent_key, json.dumps(recent), ex=3600)
    if len(recent) >= _ENGINE_ERROR_THRESHOLD:
        try:
            _open_orchestrator_bug_lock(
                reason=f"Stage B ENGINE_ERROR threshold tripped for {supervisor}: {repr(exc)}",
                owner="claude-code-fleet-notify",
            )
        except Exception as buglock_exc:
            log_debug(supervisor, f"ENGINE_ERROR bug_lock open failed: {buglock_exc}")
    return event_key, len(recent)


def _evaluate_stop_discipline(r, node_id: str, observed_task_id: Optional[str]) -> StopDecision:
    blocked_on = _resolve_blocked_on(observed_task_id)
    if blocked_on:
        return StopDecision(
            wake_type=WAKE_ALLOW_STOP,
            body=f"blocked_on={blocked_on}",
        )

    supervisor = _resolve_supervisor(r, node_id) or node_id
    try:
        if _session_pause_active(r, supervisor):
            return StopDecision(wake_type=WAKE_ALLOW_STOP, body="paused_by_user")

        audit_events = _expire_stale_in_progress_projects(r, supervisor)
        projects = sorted(
            _get_session_supervised_projects(supervisor),
            key=lambda project: project.get("priority") if project.get("priority") is not None else 999999999,
        )
        for project in projects:
            status = str(project.get("status") or "active")
            project_id = str(project.get("id"))
            if status == "completed":
                continue
            if status == "in_progress":
                continue

            active_conditions = _active_conditions(project)
            valid_stop = _project_has_valid_stop(project)
            if not active_conditions and project.get("user_stop_conditions"):
                log_debug(node_id, f"deprecated-only conditions on {project_id}; allowing stop")
                continue

            next_ready = _get_session_next_ready(supervisor, project_id=project_id)
            if next_ready:
                task_id = next_ready.get("task_id") or next_ready.get("id")
                task_title = str(next_ready.get("description") or "")[:80]
                return StopDecision(
                    wake_type=WAKE_WITH_QUEUE,
                    project_id=project_id,
                    phase_id=next_ready.get("phase_id"),
                    task_id=task_id,
                    task_title_short=task_title,
                    task_priority=next_ready.get("priority"),
                    resume_context_pointer=f"/api/tasks/{task_id}" if task_id else None,
                    next_action=f"Pick up {task_id} via taey-queue next or inspect {_ORCH_API_BASE}/api/tasks/{task_id}",
                    body="ready work available",
                    audit_events=audit_events,
                )

            if status == "stopped" and valid_stop:
                continue
            if valid_stop:
                continue

            return StopDecision(
                wake_type=WAKE_REASON_REQUIRED,
                project_id=project_id,
                available_conditions=[
                    {
                        "condition_id": cond.get("id"),
                        "version": cond.get("version"),
                        "label": cond.get("label"),
                    }
                    for cond in active_conditions
                ],
                next_action=f"Set a stop reason for {project_id} with taey-stop-reason set {project_id} --condition <prefix> --detail \"...\"",
                body="no ready work and no valid stop_reason",
                audit_events=audit_events,
            )

        return StopDecision(wake_type=WAKE_ALLOW_STOP, audit_events=audit_events)
    except Exception as exc:
        error_key, recent_count = _record_engine_error(r, supervisor, exc)
        return StopDecision(
            wake_type=WAKE_ENGINE_ERROR,
            body=f"{type(exc).__name__}: {exc}",
            next_action="Investigate orchestrator connectivity and clear the bug-lock only after root cause is fixed.",
            error_key=error_key,
            audit_events=[f"recent_engine_errors={recent_count}"],
        )


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


def _resolve_blocked_on(task_id: Optional[str]) -> Optional[str]:
    """Return the OrchTask.blocked_on value for ``task_id``, if any."""
    if not task_id:
        return None
    url = f"http://127.0.0.1:5002/api/tasks/{task_id}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    blocked_on = payload.get("blocked_on")
    if blocked_on in (None, "", "null"):
        return None
    return str(blocked_on)


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


# Atomic compare-and-clear: only delete current_task + last_outcome if the
# current_task value's task_id still matches what we observed when we built
# the summary. Without this, a concurrent dispatch() that wrote a fresh
# task_id between our read and our delete would be silently wiped (Gaia
# code audit 2026-05-26, TIER 1 collapse of five findings).
#
# Returns 1 if the clear executed (task_id matched), 0 if the clear was
# skipped (task_id mismatch — a newer dispatch is already in flight, do
# not interfere). The marker key (KEYS[3]) is set to "1" with a short TTL
# only when the clear actually fires — orch-watch's DEL handler reads
# this to distinguish a Stop-hook done-clear from a supervisor force-clear
# (Gaia orch-watch #2).
_CAS_CLEAR_DONE_LUA = """
local cur = redis.call('GET', KEYS[1])
if not cur then return 0 end
local ok, task = pcall(cjson.decode, cur)
if not ok then return 0 end
if task['task_id'] == ARGV[1] then
    redis.call('DEL', KEYS[1])
    redis.call('DEL', KEYS[2])
    redis.call('SET', KEYS[3], '1', 'EX', 30)
    return 1
end
return 0
"""


def _notify_supervisor_of_stop(r, node_id: str, supervisor: str) -> None:
    """Push a peer_idle message to the supervisor's inbox when this worker
    stops. Body includes the just-completed task summary + outcome enum,
    so the supervisor sees the result inline without context-switching to
    the worker pane.

    Dedup is keyed per ``(node_id, task_id)`` — back-to-back Stops on the
    SAME task dedup; a Stop after a re-dispatch on the SAME worker but a
    DIFFERENT task fires fresh (Logos contract #3 / Gaia dispatch #2).

    Persistence rule (Gaia, Phase A consultation 2026-05-26): clear
    current_task ONLY when the outcome is explicitly ``done``. Any other
    outcome leaves current_task as the "previous dispatch did not complete
    cleanly" signal.

    Atomicity rule (Gaia code audit 2026-05-26, TIER 1): the done-clear
    runs as a Lua compare-and-delete keyed on the observed task_id, so a
    concurrent dispatch() that wrote a fresh task_id between our read and
    our delete is NOT silently wiped. The done-clear also writes
    ``taey:<node>:last_clear_was_done`` (30s TTL marker) so orch-watch's
    DEL handler can distinguish done-clear from supervisor force-clear.
    """
    try:
        from notifications.inbox import inbox_key, state_key

        summary, outcome = _current_task_summary(r, node_id)

        # Capture observed task BEFORE doing anything else — the Lua clear
        # below uses task_id to compare-and-swap. If a concurrent dispatch()
        # arrives between this read and the Lua, the Lua sees the new
        # task_id and skips the clear.
        observed_task = None
        observed_task_id = None
        try:
            cur = r.get(state_key(node_id, "current_task"))
            if cur:
                observed_task = json.loads(cur)
                observed_task_id = observed_task.get("task_id")
        except Exception:
            observed_task = None
            observed_task_id = None

        # Audit fix (Gaia system-integration sign-off 2026-05-26, TIER-1):
        # peer_idle MUST be self-describing — adopters without conductor-
        # grade dispatch-state binding (treasurer, x-claude, external
        # users) need task_id as a structured field, not buried in body
        # text where it has to be regex-extracted. Also surface
        # task_description / supervisor / started_at + last_outcome
        # details so the supervisor can update their plan-graph status
        # directly from the wire without re-querying current_task (which
        # is about to be cleared).
        observed_outcome_struct = None
        try:
            lo = r.get(state_key(node_id, "last_outcome"))
            if lo:
                observed_outcome_struct = json.loads(lo)
        except Exception:
            observed_outcome_struct = None

        dedup_suffix = observed_task_id or "no-task"
        dedup = f"taey:peer-idle-notified:{node_id}:{dedup_suffix}"
        if r.exists(dedup):
            return

        decision = _evaluate_stop_discipline(r, node_id, observed_task_id)
        if decision.wake_type == WAKE_ALLOW_STOP:
            msg = f"suppressed PEER_IDLE for {node_id}: {decision.body or 'allow_stop'}"
            print(msg, file=sys.stderr)
            log_debug(node_id, msg)
            if outcome == "done" and observed_task_id:
                try:
                    cleared = r.eval(
                        _CAS_CLEAR_DONE_LUA, 3,
                        state_key(node_id, "current_task"),
                        state_key(node_id, "last_outcome"),
                        state_key(node_id, "last_clear_was_done"),
                        observed_task_id,
                    )
                    if not cleared:
                        log_debug(node_id, f"STOP CAS skipped clear for allow_stop observed={observed_task_id}")
                except Exception as cas_exc:
                    log_debug(node_id, f"STOP CAS clear failed: {cas_exc}")
            return

        if summary:
            body = f"{node_id} stopped — {summary}"
            priority = "high" if outcome in ("error", "interrupted") else "normal"
        else:
            body = f"{node_id} stopped — no current task recorded"
            priority = "low"

        msg_obj = {
            "from": node_id,
            "type": "wake",
            "wake_type": decision.wake_type,
            "body": body if not decision.body else f"{body}; {decision.body}",
            "outcome": outcome,  # enum: done|error|interrupted|unknown
            "priority": "high" if decision.wake_type in (WAKE_REASON_REQUIRED, WAKE_ENGINE_ERROR) else priority,
            "msg_id": f"wake-{node_id}-{dedup_suffix}-{int(time.time())}",
            "timestamp": time.time(),
            "project_id": decision.project_id,
            "phase_id": decision.phase_id,
            "task_id": decision.task_id,
            "task_priority": decision.task_priority,
            "stopped_task_id": observed_task_id,
            "task_title_short": decision.task_title_short,
            "resume_context_pointer": decision.resume_context_pointer,
            "available_conditions": decision.available_conditions,
            "next_action": decision.next_action,
            "error_key": decision.error_key,
            "audit_events": decision.audit_events,
            # Self-describing fields (v0.2.3+, Gaia TIER-1 fix):
            "task_description": (observed_task.get("description") if observed_task else None),
            "task_supervisor": (observed_task.get("supervisor") if observed_task else None),
            "task_started_at": (observed_task.get("started_at") if observed_task else None),
            "outcome_details": (observed_outcome_struct.get("details") if observed_outcome_struct else None),
        }
        # If the task carried a state_file pointer, thread its current
        # SHA so the supervisor can verify the state file hasn't drifted
        # from what the worker last wrote (companion to orch-cron's
        # hash-on-fire sidecar from Phase C).
        if observed_task and observed_task.get("state_file"):
            state_file = observed_task["state_file"]
            msg_obj["state_file"] = state_file
            try:
                with open(state_file + ".meta.json") as _mf:
                    msg_obj["state_file_sha"] = json.load(_mf).get("last_fire_log_hash")
            except Exception:
                pass

        msg = json.dumps(msg_obj)
        r.lpush(inbox_key(supervisor), msg)
        r.set(dedup, "1", ex=60)

        # Clear ONLY on confirmed completion, AND only if the observed
        # task_id still matches what's in Redis (CAS). If a fresh dispatch
        # arrived between our read and this point, the Lua skips the
        # clear — its new task_id survives.
        if outcome == "done" and observed_task_id:
            try:
                cleared = r.eval(
                    _CAS_CLEAR_DONE_LUA, 3,
                    state_key(node_id, "current_task"),
                    state_key(node_id, "last_outcome"),
                    state_key(node_id, "last_clear_was_done"),
                    observed_task_id,
                )
                if not cleared:
                    log_debug(node_id,
                              f"STOP CAS skipped clear — current_task task_id no longer "
                              f"matches observed={observed_task_id}; newer dispatch in flight.")
            except Exception as cas_exc:
                log_debug(node_id, f"STOP CAS clear failed: {cas_exc}")

        log_debug(node_id,
                  f"STOP: notified supervisor={supervisor} outcome={outcome} "
                  f"observed_task_id={observed_task_id} body=\"{body}\"")
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
