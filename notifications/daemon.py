#!/usr/bin/env python3
"""Notification delivery daemon — one per machine, handles all local tmux sessions.

The daemon is the stopped-session delivery path. It periodically discovers tmux
sessions, checks whether a session is safe to inject into, and submits a Redis
pointer with the same tmux choreography as ``scripts/tmux-send``. Messages remain
queued until the recipient hook drains them after a real prompt/tool event.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# Ensure the repo root is importable when this file is executed as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hashlib
import json
import socket

from notifications.inbox import (
    WAKE_TYPES,
    format_notification_block,
    has_pending_messages,
    inbox_key,
    is_node_idle,
    key_prefix,
    notifications_key,
    orch_key,
    state_key,
)
from notifications.handoff import (
    mark_session_machine,
    record_delivery_signal,
    session_machine_key,
    validate_handoff_activation,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [notify-daemon] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 3
# Per-session pointer-inject backoff: a session whose safe-to-inject predicate
# stays true and whose inbox does NOT change (wedged / non-submitting / broken
# Stop hook) is re-injected at most once per this interval, instead of every poll_interval.
# A NEW message (inbox signature changes) injects promptly; a session that
# submits the pointer clears idle and is never re-injected. Prevents the
# every-3s keystroke hammer that disrupts wedged sessions fleet-wide.
POINTER_INJECT_BACKOFF_SECS = 90
HANDOFF_VALIDATION_TIMEOUT_SECS = 5.0
HANDOFF_VALIDATION_SHUTDOWN_GRACE_SECS = 0.1
REDIS_SOCKET_TIMEOUT_SECS = 2.0
DEFAULT_INJECT_FAILURE_ESCALATE_AFTER = 3
DEFAULT_INJECT_FAILURE_ESCALATION_TTL_SECS = 900
MAX_MESSAGE_LENGTH = 4000
DAEMON_HEARTBEAT_NODE = "_notify_daemon"
DAEMON_HEARTBEAT_SUFFIX = "heartbeat"
DAEMON_DELIVERY_PROGRESS_SUFFIX = "delivery_progress"
DEFAULT_DAEMON_HEARTBEAT_INTERVAL_SECS = 3.0
DEFAULT_DELIVERY_PROGRESS_MAX_AGE_SECS = 15.0
DEFAULT_RECONCILE_AT_REST_GRACE_SECS = 60
DEFAULT_FRESH_TOOL_RUNNING_MAX_AGE_SECS = 3600
COMPOSER_OCCUPANCY_SUFFIX = "composer_occupancy"
AT_REST_PANE_FINGERPRINT_SUFFIX = "at_rest_pane_fingerprint"
DEFAULT_AT_REST_PANE_FINGERPRINT_TTL_SECS = 120
COMPOSER_RESTING_REGION_NONBLANK_LINES = 12
COMPOSER_PROMPT_MARKERS = ("❯", "›")
COMPOSER_IGNORED_PROMPT_PREFIXES = (
    "use /skills to list available skills",
    "how is claude doing this session?",
    "find and fix a bug in @filename",
    "write tests for @filename",
    "explain this codebase",
    "implement {feature}",
    "improve documentation in @filename",
    "summarize recent commits",
)
ACTIVE_TURN_MARKERS = (
    "esc to interrupt",
    "escape to interrupt",
)
SUBMIT_VERIFY_RETRIES = 2
SUBMIT_VERIFY_SETTLE_SECS = 0.25
TURN_STATE_IDLE = "IDLE"
TURN_STATE_IN_TURN_WORKING = "IN_TURN_WORKING"
TURN_STATE_IN_TURN_STALLED = "IN_TURN_STALLED"
TURN_STATE_COMPOSER_OCCUPIED = "COMPOSER_OCCUPIED"


class _DaemonShutdown(BaseException):
    def __init__(self, signum: int):
        super().__init__(f"daemon shutdown signal {signum}")
        self.signum = signum


class _TmuxInjection:
    def __init__(self, session_name: str, sequence: str, expected_text: str | None = None):
        self.session_name = session_name
        self.sequence = sequence
        self.expected_text = expected_text or ""
        self.phase = "created"
        self.drained = False
        self._lock = threading.RLock()

    def set_phase(self, phase: str) -> None:
        with self._lock:
            if not self.drained:
                self.phase = phase

    def send_keys(self, *keys: str) -> bool:
        return _run_tmux_command(["tmux", "send-keys", "-t", self.session_name, *keys])

    def load_buffer(self, message: str) -> bool:
        return _run_tmux_command(["tmux", "load-buffer", "-"], input_text=message)

    def paste_buffer(self) -> bool:
        return _run_tmux_command(["tmux", "paste-buffer", "-t", self.session_name, "-p", "-d"])

    def resend_submit(self) -> bool:
        if self.sequence == "grok":
            return self.send_keys("Enter")
        if not self.send_keys("Enter"):
            return False
        time.sleep(0.1)
        return self.send_keys("-H", "1b", "5b", "31", "33", "75")

    def drain(self) -> bool:
        with self._lock:
            if self.drained:
                return True
            phase = self.phase
            self.drained = True
            if phase == "submitted":
                return True
            if phase in {"created", "cleared"}:
                return self.send_keys("C-u", "C-k")
            if self.sequence == "grok":
                return self.send_keys("Enter")
            commands: list[tuple[str, ...]]
            if phase == "text_sent":
                commands = (
                    ("Escape",),
                    ("Enter",),
                    ("-H", "1b", "5b", "31", "33", "75"),
                )
            elif phase == "escape_sent":
                commands = (
                    ("Enter",),
                    ("-H", "1b", "5b", "31", "33", "75"),
                )
            elif phase == "legacy_enter_sent":
                commands = (("-H", "1b", "5b", "31", "33", "75"),)
            else:
                commands = (("C-u", "C-k"),)
            ok = True
            for keys in commands:
                ok = self.send_keys(*keys) and ok
            return ok


_SHUTDOWN_REQUESTED = threading.Event()
_ACTIVE_INJECTION_LOCK = threading.RLock()
_ACTIVE_INJECTION: _TmuxInjection | None = None


def _run_tmux_command(
    cmd: list[str],
    *,
    input_text: str | None = None,
    timeout: float = 20,
) -> bool:
    result = subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        logger.error(
            "tmux command failed: %s",
            (result.stderr or result.stdout or " ".join(cmd)).strip(),
        )
        return False
    return True


def _set_active_injection(injection: _TmuxInjection | None) -> None:
    global _ACTIVE_INJECTION
    with _ACTIVE_INJECTION_LOCK:
        _ACTIVE_INJECTION = injection


def _drain_active_injection() -> bool:
    with _ACTIVE_INJECTION_LOCK:
        injection = _ACTIVE_INJECTION
    if injection is None:
        return True
    return injection.drain()


def _handle_shutdown_signal(signum: int, _frame) -> None:
    _SHUTDOWN_REQUESTED.set()
    try:
        drained = _drain_active_injection()
    except Exception as exc:
        drained = False
        logger.error("active injection drain failed during signal %s: %s", signum, exc)
    logger.info("Received signal %s; active injection drained=%s", signum, drained)
    raise _DaemonShutdown(signum)


def _install_shutdown_signal_handlers() -> dict[int, object]:
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous: dict[int, object] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, _handle_shutdown_signal)
    return previous


def _restore_shutdown_signal_handlers(previous: dict[int, object]) -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    for signum, handler in previous.items():
        signal.signal(signum, handler)


class _HandoffValidationJob:
    def __init__(self, redis_client, *, prefix: str, timeout_sec: float):
        self.redis_client = redis_client
        self.prefix = prefix
        self.timeout_sec = timeout_sec
        self.started_at = time.monotonic()
        self.warned = False
        self.updated = 0
        self.error: Exception | None = None
        self.done = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name="notify-handoff-activation-validation",
            daemon=True,
        )

    def start(self) -> "_HandoffValidationJob":
        self.thread.start()
        return self

    def _run(self) -> None:
        try:
            self.updated = validate_handoff_activation(
                self.redis_client,
                prefix=self.prefix,
                timeout_sec=None,
            )
        except Exception as exc:
            self.error = exc
        finally:
            self.done.set()


class _DaemonDeliveryProgress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cursor = 0
        self._last_monotonic = time.monotonic()

    def mark(self) -> int:
        with self._lock:
            self._cursor += 1
            self._last_monotonic = time.monotonic()
            return self._cursor

    def age(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_monotonic


def _consume_handoff_validation_job(
    job: _HandoffValidationJob | None,
    *,
    wait_sec: float = 0.0,
) -> _HandoffValidationJob | None:
    if job is None:
        return None
    if wait_sec > 0:
        job.thread.join(wait_sec)
    if not job.done.is_set():
        return job
    if job.error is not None:
        logger.error("handoff activation validation failed: %s", job.error)
    return None


def _advance_handoff_validation_job(
    job: _HandoffValidationJob | None,
    redis_client,
    *,
    prefix: str,
    timeout_sec: float,
) -> _HandoffValidationJob | None:
    job = _consume_handoff_validation_job(job)
    if job is None:
        return _HandoffValidationJob(redis_client, prefix=prefix, timeout_sec=timeout_sec).start()
    elapsed = time.monotonic() - job.started_at
    if not job.warned and elapsed >= timeout_sec:
        logger.error(
            "handoff activation validation still running after %.3fs; heartbeat/delivery continue",
            timeout_sec,
        )
        job.warned = True
    return job


def get_local_tmux_sessions() -> list[str]:
    """List all tmux sessions on this machine."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return [s.strip() for s in result.stdout.strip().splitlines() if s.strip()]
    except Exception:
        pass
    return []


def _tmux_pane_tail(session_name: str, *, lines: int = 80) -> str:
    ok, text = _capture_tmux_pane_tail(session_name, lines=lines)
    return text if ok else ""


def _capture_tmux_pane_tail(session_name: str, *, lines: int = 80) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session_name, "-S", f"-{lines}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return True, result.stdout or ""
    except Exception as exc:
        logger.debug("tmux pane capture failed for %s: %s", session_name, exc)
    return False, ""


def _recent_nonblank_pane_lines(pane_text: str, *, limit: int) -> list[str]:
    nonblank_lines = [line for line in (pane_text or "").splitlines() if line.strip()]
    return nonblank_lines[-limit:]


def _strip_composer_box_edges(line: str) -> str:
    text = line.strip()
    if text.startswith("│"):
        text = text[1:]
    if text.endswith("│"):
        text = text[:-1]
    return text.strip()


def _usable_composer_text(text: str) -> str:
    candidate = " ".join(str(text or "").split())
    if not candidate:
        return ""
    lowered = candidate.lower()
    if any(lowered.startswith(prefix) for prefix in COMPOSER_IGNORED_PROMPT_PREFIXES):
        return ""
    return candidate


def _composer_text_matches_expected_submission(composer_text: str, expected_text: str) -> bool:
    composer = " ".join(str(composer_text or "").split())
    expected = " ".join(str(expected_text or "").split())
    if not composer or not expected:
        return False
    return expected.startswith(composer) or composer.startswith(expected)


def _marker_position(inner: str) -> tuple[int, str] | None:
    marker_positions = [
        (inner.find(marker), marker)
        for marker in COMPOSER_PROMPT_MARKERS
        if marker in inner
    ]
    marker_positions = [(pos, marker) for pos, marker in marker_positions if pos >= 0]
    if not marker_positions:
        return None
    return min(marker_positions, key=lambda item: item[0])


def _composer_text_from_box_lines(box_lines: list[str]) -> str:
    for index, raw_line in enumerate(box_lines):
        inner = _strip_composer_box_edges(raw_line)
        marker_found = _marker_position(inner)
        if marker_found is None:
            continue
        marker_pos, marker = marker_found
        inline = _usable_composer_text(inner[marker_pos + len(marker):])
        if inline:
            return inline[:160]
        for continuation in box_lines[index + 1:]:
            text = _usable_composer_text(_strip_composer_box_edges(continuation))
            if text:
                return text[:160]
        return ""
    return ""


def _composer_box_lines(recent_lines: list[str]) -> list[str] | None:
    box_lines: list[str] = []
    in_box = False
    for raw_line in reversed(recent_lines):
        stripped = raw_line.strip()
        if not in_box:
            if stripped.startswith("╰") and stripped.endswith("╯"):
                in_box = True
            continue
        if stripped.startswith("╭") and stripped.endswith("╮"):
            return list(reversed(box_lines))
        if stripped.startswith("│") and stripped.endswith("│"):
            box_lines.append(raw_line)
    return None


def _composer_text_from_pane(pane_text: str) -> str:
    recent_lines = _recent_nonblank_pane_lines(
        pane_text,
        limit=COMPOSER_RESTING_REGION_NONBLANK_LINES,
    )
    box_lines = _composer_box_lines(recent_lines)
    if box_lines is not None:
        return _composer_text_from_box_lines(box_lines)

    for index in range(len(recent_lines) - 1, -1, -1):
        raw_line = recent_lines[index]
        inner = _strip_composer_box_edges(raw_line)
        marker_found = _marker_position(inner)
        if marker_found is None:
            continue
        marker_pos, marker = marker_found
        inline = _usable_composer_text(inner[marker_pos + len(marker):])
        if inline:
            return inline[:160]
        if not raw_line.strip().startswith("│"):
            return ""
        for continuation in recent_lines[index + 1:]:
            continuation_inner = _strip_composer_box_edges(continuation)
            if _marker_position(continuation_inner) is not None:
                break
            text = _usable_composer_text(continuation_inner)
            if text:
                return text[:160]
        return ""
    return ""


def _pane_shows_active_turn(pane_text: str) -> bool:
    recent_lines = _recent_nonblank_pane_lines(
        pane_text,
        limit=COMPOSER_RESTING_REGION_NONBLANK_LINES,
    )
    for line in recent_lines:
        normalized = " ".join(line.lower().split())
        if any(marker in normalized for marker in ACTIVE_TURN_MARKERS):
            return True
    return False


def _pane_shows_resting_composer_box(pane_text: str) -> bool:
    recent_lines = _recent_nonblank_pane_lines(
        pane_text,
        limit=COMPOSER_RESTING_REGION_NONBLANK_LINES,
    )
    box_lines = _composer_box_lines(recent_lines)
    if box_lines is None:
        return False
    return any(_marker_position(_strip_composer_box_edges(line)) is not None
               for line in box_lines)


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fresh_tool_running(r, node_id: str, *, now: float, max_age_sec: int) -> bool:
    if not r.exists(state_key(node_id, "tool_running")):
        return False
    observed_at = _float_or_none(r.get(state_key(node_id, "tool_running_at")))
    if observed_at is None:
        observed_at = _float_or_none(r.get(state_key(node_id, "last_tool_activity")))
    if observed_at is None:
        return False
    return max(0.0, now - observed_at) < max_age_sec


def _fresh_turn_progress(r, node_id: str, *, now: float, max_age_sec: int) -> bool:
    observed_at = _float_or_none(r.get(state_key(node_id, "last_tool_activity")))
    if observed_at is None:
        observed_at = _float_or_none(r.get(state_key(node_id, "tool_running_at")))
    if observed_at is None:
        return False
    return max(0.0, now - observed_at) < max_age_sec


def _delivery_readiness(
    r,
    node_id: str,
    session_name: str,
    *,
    now: float,
    pane_text: str | None = None,
    progress_sec: int = DEFAULT_FRESH_TOOL_RUNNING_MAX_AGE_SECS,
) -> tuple[bool, str]:
    if pane_text is None:
        pane_text = _tmux_pane_tail(session_name)
    if _pane_shows_active_turn(pane_text):
        if _fresh_turn_progress(
            r,
            node_id,
            now=now,
            max_age_sec=max(1, int(progress_sec)),
        ):
            return False, TURN_STATE_IN_TURN_WORKING
        return False, TURN_STATE_IN_TURN_STALLED
    if not is_node_idle(r, node_id):
        return False, TURN_STATE_IN_TURN_WORKING
    if _composer_text_from_pane(pane_text):
        return False, TURN_STATE_COMPOSER_OCCUPIED
    return True, TURN_STATE_IDLE


def _message_timestamp(raw: object) -> float | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _float_or_none(payload.get("timestamp"))


def _pending_messages_old_enough(r, node_id: str, *, now: float, grace_sec: int) -> bool:
    queue_keys = (
        inbox_key(node_id),
        notifications_key(node_id),
        orch_key(node_id),
    )
    saw_message = False
    for key in queue_keys:
        for raw in r.lrange(key, 0, -1):
            saw_message = True
            timestamp = _message_timestamp(raw)
            if timestamp is None:
                continue
            if max(0.0, now - timestamp) <= grace_sec:
                return False
    return saw_message


def _pane_fingerprint(pane_text: str) -> str:
    return hashlib.sha1(
        str(pane_text or "").encode("utf-8", errors="surrogatepass")
    ).hexdigest()


def _pane_content_stable(r, node_id: str, pane_text: str, *, ttl_sec: int) -> bool:
    key = state_key(node_id, AT_REST_PANE_FINGERPRINT_SUFFIX)
    fingerprint = _pane_fingerprint(pane_text)
    previous = r.get(key)
    r.set(key, fingerprint, ex=max(1, int(ttl_sec)))
    return previous == fingerprint


def _verify_tmux_submission_consumed(
    injection: _TmuxInjection,
    *,
    retries: int = SUBMIT_VERIFY_RETRIES,
) -> bool:
    for attempt in range(max(int(retries), 0) + 1):
        ok, pane_text = _capture_tmux_pane_tail(injection.session_name)
        if not ok:
            logger.error(
                "tmux injection submit verification failed for %s: capture-pane failed",
                injection.session_name,
            )
            return False
        composer_text = _composer_text_from_pane(pane_text)
        if not composer_text:
            return True
        if not _composer_text_matches_expected_submission(
            composer_text,
            injection.expected_text,
        ):
            logger.info(
                "tmux injection submit verification for %s ignored non-injected composer text",
                injection.session_name,
            )
            return True
        if attempt >= retries:
            logger.error(
                "tmux injection submit verification failed for %s: injected text still occupied after %d retries",
                injection.session_name,
                retries,
            )
            return False
        logger.warning(
            "tmux injection submit verification found injected text still in composer for %s; retrying submit (%d/%d)",
            injection.session_name,
            attempt + 1,
            retries,
        )
        injection.set_phase("submit_retry_sent")
        if not injection.resend_submit():
            logger.error(
                "tmux injection submit retry failed for %s",
                injection.session_name,
            )
            return False
        time.sleep(SUBMIT_VERIFY_SETTLE_SECS)
    return False


def observe_composer_occupancy(
    r,
    node_id: str,
    session_name: str,
    *,
    machine: str,
    now: float | None = None,
    pane_text: str | None = None,
) -> bool:
    if pane_text is None:
        pane_text = _tmux_pane_tail(session_name)
    if not pane_text:
        return False
    key = state_key(node_id, COMPOSER_OCCUPANCY_SUFFIX)
    composer_text = _composer_text_from_pane(pane_text)
    if not composer_text:
        r.delete(key)
        return False
    observed_at = time.time() if now is None else float(now)
    r.set(
        key,
        json.dumps(
            {
                "occupied": True,
                "observed_at": observed_at,
                "machine": machine,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ),
    )
    return True


def reconcile_idle_at_rest(
    r,
    node_id: str,
    session_name: str,
    *,
    now: float | None = None,
    grace_sec: int = DEFAULT_RECONCILE_AT_REST_GRACE_SECS,
    tool_running_max_age_sec: int = DEFAULT_FRESH_TOOL_RUNNING_MAX_AGE_SECS,
) -> bool:
    """Restore idle=1 when the pane is clearly at rest with old pending mail."""
    if is_node_idle(r, node_id):
        return False
    current_time = time.time() if now is None else float(now)
    if _fresh_tool_running(
        r,
        node_id,
        now=current_time,
        max_age_sec=max(1, int(tool_running_max_age_sec)),
    ):
        return False
    if not _pending_messages_old_enough(
        r,
        node_id,
        now=current_time,
        grace_sec=max(0, int(grace_sec)),
    ):
        return False
    pane_text = _tmux_pane_tail(session_name)
    if not pane_text:
        return False
    if _pane_shows_active_turn(pane_text):
        return False
    if not _pane_shows_resting_composer_box(pane_text):
        return False
    if not _pane_content_stable(
        r,
        node_id,
        pane_text,
        ttl_sec=max(
            DEFAULT_AT_REST_PANE_FINGERPRINT_TTL_SECS,
            max(0, int(grace_sec)) * 2,
        ),
    ):
        return False
    r.set(state_key(node_id, "idle"), "1")
    logger.warning("Reconciled idle=1 for %s at resting composer", node_id)
    try:
        from notifications.trace import trace
        trace(r, "idle_set", node=node_id, src="at_rest_reconcile")
    except Exception:
        pass
    return True


def _clip_message(message: str) -> str:
    if len(message) <= MAX_MESSAGE_LENGTH:
        return message
    return message[: MAX_MESSAGE_LENGTH - 28].rstrip() + "\n...[notification block truncated]"


def _send_sequence_for_session(session_name: str) -> str:
    override = os.environ.get("SEND_SEQUENCE", "").strip()
    if override:
        return override
    return "grok" if session_name.endswith("-grok") else "claude"


def inject_via_tmux(session_name: str, message: str) -> bool:
    """Inject a message into a local Claude Code tmux session."""
    sequence = _send_sequence_for_session(session_name)
    injection = _TmuxInjection(session_name, sequence, message)
    _set_active_injection(injection)

    try:
        if not injection.send_keys("C-u", "C-k"):
            return False
        injection.set_phase("cleared")
        time.sleep(0.1)

        if sequence == "grok":
            if not injection.load_buffer(message):
                return False
            if not injection.paste_buffer():
                return False
            injection.set_phase("text_sent")
            time.sleep(0.5)
            if not injection.send_keys("Enter"):
                return False
            time.sleep(SUBMIT_VERIFY_SETTLE_SECS)
        else:
            if not injection.send_keys("--", message):
                return False
            injection.set_phase("text_sent")
            time.sleep(0.5)
            if not injection.send_keys("Escape"):
                return False
            injection.set_phase("escape_sent")
            time.sleep(0.2)
            if not injection.send_keys("Enter"):
                return False
            injection.set_phase("legacy_enter_sent")
            time.sleep(0.1)
            if not injection.send_keys("-H", "1b", "5b", "31", "33", "75"):
                return False
            time.sleep(SUBMIT_VERIFY_SETTLE_SECS)
        if not _verify_tmux_submission_consumed(injection):
            return False
        injection.set_phase("submitted")
        logger.info("OK: %s (local, sequence=%s)", session_name, sequence)
        return True
    except _DaemonShutdown:
        raise
    except Exception as exc:
        logger.error("tmux injection exception for %s: %s", session_name, exc)
        try:
            injection.drain()
        except Exception:
            pass
        return False
    finally:
        with _ACTIVE_INJECTION_LOCK:
            if _ACTIVE_INJECTION is injection:
                _set_active_injection(None)

def _resolve_supervisor(r, node_id: str) -> str | None:
    from notifications.targets import resolve_supervisor

    return resolve_supervisor(r, node_id)


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def _int_or_zero(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def write_daemon_heartbeat(r, machine: str, *, now: float | None = None) -> str:
    ts = time.time() if now is None else float(now)
    value = f"{ts:.6f}+{machine}"
    r.set(state_key(DAEMON_HEARTBEAT_NODE, DAEMON_HEARTBEAT_SUFFIX), value)
    return value


def write_daemon_delivery_progress(
    r,
    machine: str,
    *,
    cursor: int,
    now: float | None = None,
) -> str:
    ts = time.time() if now is None else float(now)
    value = f"{ts:.6f}+{machine}+{int(cursor)}"
    r.set(state_key(DAEMON_HEARTBEAT_NODE, DAEMON_DELIVERY_PROGRESS_SUFFIX), value)
    return value


def _connect_redis(redis_lib, redis_host: str, redis_port: int):
    return redis_lib.Redis(
        host=redis_host,
        port=redis_port,
        decode_responses=True,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECS,
        socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECS,
    )


def _mark_daemon_delivery_progress(
    r,
    progress: _DaemonDeliveryProgress,
    machine: str,
) -> None:
    cursor = progress.mark()
    try:
        write_daemon_delivery_progress(r, machine, cursor=cursor, now=time.time())
    except Exception as exc:
        logger.error("daemon delivery progress write failed: %s", exc)


def _start_daemon_heartbeat_thread(
    redis_lib,
    redis_host: str,
    redis_port: int,
    machine: str,
    *,
    interval_sec: float,
    delivery_progress: _DaemonDeliveryProgress,
    delivery_progress_max_age_sec: float,
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    heartbeat_redis = _connect_redis(redis_lib, redis_host, redis_port)

    def beat() -> None:
        stalled_logged = False
        while not stop_event.is_set():
            progress_age = delivery_progress.age()
            if progress_age > delivery_progress_max_age_sec:
                if not stalled_logged:
                    logger.critical(
                        "daemon delivery loop progress stalled for %.3fs; heartbeat withheld",
                        progress_age,
                    )
                    stalled_logged = True
                stop_event.wait(interval_sec)
                continue
            stalled_logged = False
            try:
                write_daemon_heartbeat(heartbeat_redis, machine, now=time.time())
            except Exception as exc:
                logger.error("daemon heartbeat write failed: %s", exc)
            stop_event.wait(interval_sec)

    try:
        write_daemon_heartbeat(heartbeat_redis, machine, now=time.time())
    except Exception as exc:
        logger.error("daemon heartbeat write failed: %s", exc)

    thread = threading.Thread(
        target=beat,
        name="notify-daemon-heartbeat",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _record_pointer_inject_result(r, node_id: str, *, ok: bool,
                                  machine: str, now: float) -> None:
    count_key = state_key(node_id, "pointer_inject_fail_count")
    notice_key = state_key(node_id, "pointer_inject_failure_notified")
    if ok:
        r.delete(count_key, notice_key)
        return

    ttl_secs = _int_env(
        "INJECT_FAILURE_ESCALATION_TTL_SECS",
        DEFAULT_INJECT_FAILURE_ESCALATION_TTL_SECS,
    )
    count = _int_or_zero(r.get(count_key)) + 1
    r.set(count_key, str(count), ex=ttl_secs)

    threshold = _int_env(
        "INJECT_FAILURE_ESCALATE_AFTER",
        DEFAULT_INJECT_FAILURE_ESCALATE_AFTER,
    )
    if count < threshold or r.exists(notice_key):
        return

    supervisor = _resolve_supervisor(r, node_id)
    body = (
        f"{node_id} pointer injection failed {count} consecutive times on {machine}; "
        "messages remain queued in Redis."
    )
    if supervisor and supervisor != node_id:
        msg = json.dumps({
            "from": node_id,
            "type": "inject_failure",
            "body": body,
            "priority": "high",
            "msg_id": f"inject-failure-{node_id}-{int(now)}",
            "timestamp": now,
            "failure_count": count,
            "machine": machine,
        })
        r.lpush(inbox_key(str(supervisor)), msg)
        logger.error("Pointer injection failure escalation: %s -> %s", node_id, supervisor)
    else:
        logger.error("Pointer injection failure escalation: %s", body)
    r.set(notice_key, "1", ex=ttl_secs)


def build_pointer_summary(r, node_id: str) -> str | None:
    """Peek inbox + notifications, return a short summary pointing to Redis.

    Does NOT pop messages. The recipient reads them via the PostToolUse hook
    on their next tool call, which displays full content via additionalContext.
    """
    inbox = inbox_key(node_id)
    notif_key = notifications_key(node_id)

    inbox_raw = r.lrange(inbox, -10, -1)  # tail = oldest first
    notif_raw = r.lrange(notif_key, 0, 9)

    total = r.llen(inbox) + r.llen(notif_key)
    if total == 0:
        return None

    wake_messages = []
    for raw in reversed(inbox_raw):
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if msg.get("wake_type") in WAKE_TYPES:
            wake_messages.append(msg)
    for raw in notif_raw:
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if msg.get("wake_type") in WAKE_TYPES:
            wake_messages.append(msg)
    if wake_messages:
        return _clip_message(
            format_notification_block(
                wake_messages,
                header="=== WAKE ===",
            )
        )

    senders = set()
    first_type = None

    for raw in notif_raw:
        try:
            msg = json.loads(raw)
            senders.add(msg.get('platform', msg.get('from', 'unknown')))
            if not first_type:
                first_type = msg.get('status', msg.get('type', 'message')).upper()
        except Exception:
            pass

    for raw in reversed(inbox_raw):
        try:
            msg = json.loads(raw)
            senders.add(msg.get('from', 'unknown'))
            if not first_type:
                first_type = msg.get('type', 'message').upper()
        except Exception:
            pass

    if not first_type:
        first_type = "MESSAGE"

    sender_list = sorted(senders)
    senders_str = ", ".join(sender_list[:3]) + (", ..." if len(sender_list) > 3 else "")

    return (
        f"[NOTIFY] You have {total} messages (from {senders_str}; first={first_type}). "
        f"Read with: redis-cli -h 127.0.0.1 LRANGE {inbox} 0 -1 "
        f"(or wait — your PostToolUse hook will surface them on the next tool call)."
    )


def build_grok_full_body(r, node_id: str) -> str | None:
    """Build the full-body injection text for grok-cli targets.

    Why grok is special (validated 2026-05-26 by x-claude + treasurer
    independently with 3-fact chains): grok-cli's prompt-time hook
    inheritance (via ~/.claude/settings.json) DOES fire prompt_activity.py
    + DOES drain the Redis inbox, BUT grok-cli does NOT honor the
    ``additionalContext`` field from the hook's JSON return value the way
    Claude Code does. Net: the drained message bodies never reach grok's
    LLM context; grok only sees the pointer text injected by tmux-send.

    Workaround: for grok-named targets, the daemon injects the FULL
    message bodies via tmux-send instead of the pointer. Grok then sees
    the actual content as its user prompt. The hook still drains the
    inbox on grok's submit (idempotent — bodies were already delivered
    inline). The Ctrl-U+Ctrl-K pre-clear in tmux-send v1.0.1 prevents
    accumulation if the daemon polls before grok submits.

    Returns the concatenated bodies (each prefixed with its from + type)
    or None if there's nothing to deliver. Caps total size at ~6KB to
    avoid pathological tmux injections; truncated bodies get a clear
    ``[... truncated, read full at <key>]`` suffix pointing at Redis.
    """
    inbox = inbox_key(node_id)
    notif_key = notifications_key(node_id)

    # Peek tail-first (oldest first) — same convention as build_pointer_summary.
    raw_msgs = list(reversed(r.lrange(inbox, -10, -1)))
    raw_msgs.extend(r.lrange(notif_key, 0, 9))
    if not raw_msgs:
        return None

    blocks = []
    total_bytes = 0
    MAX_BYTES = 6000

    for raw in raw_msgs:
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        sender = msg.get('from', msg.get('platform', 'unknown'))
        mtype = msg.get('type', msg.get('status', 'message')).upper()
        body = msg.get('body', '')
        if not body:
            continue
        block = f"[{mtype} from {sender}]:\n{body}"
        if total_bytes + len(block) > MAX_BYTES:
            block = (block[: MAX_BYTES - total_bytes - 80].rstrip()
                     + f"\n[... message truncated; read full at redis key {inbox}]")
            blocks.append(block)
            break
        blocks.append(block)
        total_bytes += len(block)

    if not blocks:
        return None
    return "\n\n".join(blocks)


def run_daemon(
    redis_host: str,
    redis_port: int = 6379,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
) -> None:
    """Main loop — one process handles all local tmux sessions."""
    import redis as redis_lib

    _SHUTDOWN_REQUESTED.clear()
    previous_signal_handlers = _install_shutdown_signal_handlers()
    r = _connect_redis(redis_lib, redis_host, redis_port)

    try:
        r.ping()
    except Exception as exc:
        logger.error("Cannot connect to Redis at %s:%s: %s", redis_host, redis_port, exc)
        sys.exit(1)

    machine = socket.gethostname()
    logger.info(
        "Started on %s: redis=%s:%s poll=%ss",
        machine,
        redis_host,
        redis_port,
        poll_interval,
    )
    logger.info("Using Redis key prefix: %s", key_prefix())

    handoff_validation_job: _HandoffValidationJob | None = None
    delivery_progress = _DaemonDeliveryProgress()
    delivery_progress.mark()
    heartbeat_stop, heartbeat_thread = _start_daemon_heartbeat_thread(
        redis_lib,
        redis_host,
        redis_port,
        machine,
        interval_sec=max(0.01, min(float(poll_interval), DEFAULT_DAEMON_HEARTBEAT_INTERVAL_SECS)),
        delivery_progress=delivery_progress,
        delivery_progress_max_age_sec=max(
            DEFAULT_DELIVERY_PROGRESS_MAX_AGE_SECS,
            float(poll_interval) * 3,
        ),
    )
    try:
        while not _SHUTDOWN_REQUESTED.is_set():
            try:
                local_sessions = get_local_tmux_sessions()
                local_session_set = set(local_sessions)
                _mark_daemon_delivery_progress(r, delivery_progress, machine)

                try:
                    handoff_validation_job = _advance_handoff_validation_job(
                        handoff_validation_job,
                        r,
                        prefix=key_prefix(),
                        timeout_sec=HANDOFF_VALIDATION_TIMEOUT_SECS,
                    )
                except Exception as exc:
                    logger.error("handoff activation validation failed: %s", exc)

                for session_name in local_sessions:
                    node_id = session_name
                    _mark_daemon_delivery_progress(r, delivery_progress, machine)
                    now = time.time()
                    pane_text = _tmux_pane_tail(session_name)
                    try:
                        observe_composer_occupancy(
                            r,
                            node_id,
                            session_name,
                            machine=machine,
                            now=now,
                            pane_text=pane_text,
                        )
                    except Exception as exc:
                        logger.error("composer occupancy observation failed for %s: %s",
                                     node_id, exc)
                    try:
                        mark_session_machine(
                            r,
                            prefix=key_prefix(),
                            session_id=node_id,
                            machine=machine,
                        )
                    except Exception:
                        pass

                    if not has_pending_messages(r, node_id):
                        continue
                    now = time.time()
                    if not is_node_idle(r, node_id):
                        reconcile_idle_at_rest(r, node_id, session_name, now=now)
                    ready, turn_state = _delivery_readiness(
                        r,
                        node_id,
                        session_name,
                        now=now,
                        pane_text=pane_text,
                    )
                    if not ready:
                        logger.info(
                            "Deferring notify injection for %s: turn_state=%s",
                            node_id,
                            turn_state,
                        )
                        continue

                    # Per-session inject backoff. The inbox signature changes when a
                    # new message arrives (inject promptly) but stays identical for a
                    # wedged/non-submitting session (idle never clears) — in which
                    # case re-inject at most once per POINTER_INJECT_BACKOFF_SECS
                    # instead of every poll. Prevents the keystroke hammer.
                    try:
                        inbox_sig = hashlib.sha1(
                            b"\n".join(s.encode() for s in r.lrange(inbox_key(node_id), 0, -1))
                        ).hexdigest()
                    except Exception:
                        inbox_sig = None
                    if inbox_sig is not None:
                        bo_raw = r.get(state_key(node_id, "pointer_inject_backoff"))
                        if bo_raw:
                            try:
                                bo = json.loads(bo_raw)
                                if (bo.get("sig") == inbox_sig
                                        and (now - float(bo.get("ts", 0))) < POINTER_INJECT_BACKOFF_SECS):
                                    continue  # same undrained inbox, injected recently -> back off
                            except Exception:
                                pass

                    # Grok-cli does NOT honor additionalContext from prompt hooks
                    # the way Claude Code / codex / gemini do. For *-grok targets
                    # we inject FULL message bodies via tmux instead of the
                    # pointer; for everyone else we keep the pointer pattern.
                    # Inbox still drains via the hook on grok's submit
                    # (idempotent — bodies already delivered inline).
                    # Verified 2026-05-26 by x-claude + treasurer independently
                    # with 3-fact chains showing the pointer pattern silently
                    # dropped grok's dispatched bodies.
                    if node_id.endswith("-grok"):
                        summary = build_grok_full_body(r, node_id)
                    else:
                        # POINTER ONLY — peek inbox, build summary, inject into tmux.
                        # Full bodies stay in Redis; recipient reads via hook on
                        # next tool call / prompt submit.
                        summary = build_pointer_summary(r, node_id)
                    if not summary:
                        continue

                    # DO NOT clear idle here. idle is removed ONLY by the
                    # UserPromptSubmit hook — i.e. only when the recipient actually
                    # SUBMITS the injected pointer as a prompt (= real, validated
                    # delivery). Clearing it here ASSUMED the injection landed; when an
                    # injection silently failed, idle was gone with no prompt ever
                    # submitted, so the message stranded with nothing to re-trigger
                    # delivery.
                    #
                    # Leaving idle set: a failed / unsubmitted injection simply retries
                    # on the next poll; a SUCCESSFUL one is cleared by the hook at
                    # submit-time (within one poll window), so it does not re-inject.
                    # Double-delivery is therefore prevented by the hook, not by the
                    # daemon pre-emptively clearing a flag it cannot validate.
                    logger.info("Notifying %s: %s", session_name, summary)
                    _mark_daemon_delivery_progress(r, delivery_progress, machine)
                    ok = inject_via_tmux(session_name, summary)
                    _mark_daemon_delivery_progress(r, delivery_progress, machine)
                    # Stamp the backoff on every attempt (ok or not): whether the
                    # inject lands or fails, do not re-inject this same inbox for
                    # POINTER_INJECT_BACKOFF_SECS. A new message changes inbox_sig and
                    # injects promptly; a successful submit clears idle (no re-inject).
                    if inbox_sig is not None:
                        try:
                            r.set(
                                state_key(node_id, "pointer_inject_backoff"),
                                json.dumps({"sig": inbox_sig, "ts": now}),
                                ex=POINTER_INJECT_BACKOFF_SECS * 4,
                            )
                        except Exception:
                            pass
                    try:
                        from notifications.trace import trace
                        trace(r, "inject", node=session_name, ok=ok)
                    except Exception:
                        pass
                    try:
                        record_delivery_signal(
                            r,
                            prefix=key_prefix(),
                            target_session_id=node_id,
                            signal="inject_ok" if ok else "inject_failed",
                            signal_source="notify-daemon",
                            machine=machine,
                        )
                    except Exception:
                        pass
                    try:
                        _record_pointer_inject_result(
                            r,
                            node_id,
                            ok=ok,
                            machine=machine,
                            now=now,
                        )
                    except Exception:
                        pass
                    if not ok:
                        logger.error(
                            "Injection failed for %s; message left queued, will retry",
                            session_name,
                        )

                inbox_pattern = f"{key_prefix()}:*:inbox"
                for inbox in r.scan_iter(match=inbox_pattern):
                    _mark_daemon_delivery_progress(r, delivery_progress, machine)
                    parts = str(inbox).split(":")
                    if len(parts) < 3:
                        continue
                    target_session_id = parts[-2]
                    if target_session_id in local_session_set:
                        continue
                    if not has_pending_messages(r, target_session_id):
                        continue
                    machine_key = session_machine_key(key_prefix(), target_session_id)
                    if r.get(machine_key) != machine:
                        continue
                    try:
                        record_delivery_signal(
                            r,
                            prefix=key_prefix(),
                            target_session_id=target_session_id,
                            signal="tmux_missing",
                            signal_source="notify-daemon",
                            machine=machine,
                        )
                    except Exception:
                        pass

                try:
                    from notifications.trace import trim_trace
                    trim_trace(r)
                except Exception:
                    pass
                time.sleep(poll_interval)
            except _DaemonShutdown:
                logger.info("Daemon stopped")
                break
            except KeyboardInterrupt:
                logger.info("Daemon stopped")
                break
            except Exception as exc:
                logger.error("Error: %s", exc)
                time.sleep(poll_interval)
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(1.0)
        _consume_handoff_validation_job(
            handoff_validation_job,
            wait_sec=HANDOFF_VALIDATION_SHUTDOWN_GRACE_SECS,
        )
        _restore_shutdown_signal_handlers(previous_signal_handlers)


def main() -> None:
    parser = argparse.ArgumentParser(description="Notification delivery daemon (one per machine)")
    parser.add_argument("--redis-host", default=os.environ.get("REDIS_HOST", "127.0.0.1"))
    parser.add_argument("--redis-port", type=int, default=int(os.environ.get("REDIS_PORT", "6379")))
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL)
    # Legacy args (ignored — kept for backwards compatibility with old scripts)
    parser.add_argument("--node", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--tmux-session", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    run_daemon(args.redis_host, args.redis_port, args.poll_interval)


if __name__ == "__main__":
    main()
