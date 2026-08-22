#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from fnmatch import fnmatch
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from notifications.targets import target_liveness_snapshot, validate_target_reader


class FakeRedis:
    def __init__(
        self,
        values: dict[str, object],
        lengths: dict[str, object],
        trace_entries: list[tuple[str, dict[str, str]]],
        sorted_sets: dict[str, object] | None = None,
    ):
        self._values = dict(values)
        self._lengths = dict(lengths)
        self._trace_entries = list(trace_entries)
        self._sorted_sets = dict(sorted_sets or {})

    def scan_iter(self, match: str, count: int = 1000):
        del count
        for key in sorted(self._values):
            if fnmatch(key, match):
                yield key

    def get(self, key: str) -> object | None:
        value = self._values.get(key)
        if isinstance(value, Exception):
            raise value
        return value

    def llen(self, key: str) -> int:
        value = self._lengths.get(key, 0)
        if isinstance(value, Exception):
            raise value
        return int(value)

    def exists(self, key: str) -> int:
        return int(key in self._values or key in self._lengths)

    def xrevrange(self, key: str, start: str, end: str, count: int = 2000):
        del start, end, count
        if key != "taey:notify_trace":
            return []
        return list(self._trace_entries)

    def zrangebyscore(self, key: str, minimum: str, maximum: str, withscores: bool = False):
        del maximum
        value = self._sorted_sets.get(key, [])
        if isinstance(value, Exception):
            raise value
        threshold = float(str(minimum).removeprefix("("))
        entries = []
        for entry in value:
            try:
                score = float(entry[1])
            except (TypeError, ValueError):
                entries.append(entry)
                continue
            if score > threshold:
                entries.append(entry)
        if withscores:
            return entries
        return [entry[0] for entry in entries]


def _check(name: str, condition: bool, detail: object) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {name}: {detail}")
    print(f"PASS: {name}")


def main() -> int:
    now = time.time()
    redis_client = FakeRedis(
        values={
            "check:live-empty:last_activity": str(now),
            "check:draining-seat:last_activity": str(now),
            "check:blocked-gemini:last_activity": str(now),
            "check:stale-seat:last_activity": str(now - 86400),
            "check:depth-error:last_activity": str(now),
            "check:activity-error:last_activity": RuntimeError("simulated GET failure"),
            "check:taey:last_activity": str(now - 86400),
            "check:taey:turns_open": "1",
            "check:stale-idle:last_activity": str(now - 86400),
            "check:stale-idle:idle": "1",
            "check:stale-idle:turns_open": "0",
            "check:taey-council-1:last_activity": str(now - 86400),
            "check:taey-council-1:idle": "1",
            "check:taey-council-1:turns_open": "0",
            "check:taey-council-1:seat_registration": "{}",
            "check:taey-council-1:last_drain_at": str(now),
            "check:taey-council-2:last_activity": str(now - 86400),
            "check:taey-council-2:turns_open": "1",
            "check:taey-council-3:last_activity": str(now - 86400),
            "check:taey-council-3:turns_open": "1",
            "check:taey-council-4:last_activity": str(now - 86400),
            "check:taey-council-4:turns_open": "1",
            "check:taey-council-5:last_activity": str(now - 86400),
            "check:taey-council-5:turns_open": "1",
            "check:lookalike-active:last_activity": str(now - 86400),
            "check:lookalike-active:turns_open": "1",
            "check:dead-headless:last_activity": str(now - 86400),
            "check:dead-headless:turns_open": "0",
            "check:dead-headless:seat_registration": "{}",
            "check:seat-only:turns_open": "0",
            "check:seat-only:seat_registration": "{}",
            "check:idle-piling:last_activity": str(now - 86400),
            "check:idle-piling:idle": "1",
            "check:idle-piling:turns_open": "0",
        },
        lengths={
            "check:draining-seat:inbox": 3,
            "check:blocked-gemini:inbox": 7,
            "check:depth-error:inbox": RuntimeError("simulated LLEN failure"),
            "check:dead-headless:inbox": 4,
            "check:idle-piling:inbox": 5,
            "check:taey-council-5:inbox": 5,
        },
        trace_entries=[
            ("1-0", {"ev": "drain", "node": "draining-seat", "wall": str(now)}),
        ],
        sorted_sets={
            "check:taey:active_turns": [("turn-live", now + 120)],
            "check:taey-council-2:active_turns": [("turn-expired", now - 1)],
            "check:taey-council-3:active_turns": [("turn-malformed", "not-a-score")],
            "check:taey-council-4:active_turns": RuntimeError("simulated ZRANGEBYSCORE failure"),
            "check:taey-council-5:active_turns": [("turn-live-backlog", now + 120)],
            "check:lookalike-active:active_turns": [("turn-lookalike", now + 120)],
        },
    )

    ok, error = validate_target_reader(
        redis_client,
        "codex-1",
        "check",
        tmux_sessions={"live-empty", "draining-seat", "blocked-gemini", "stale-seat"},
        registered_sessions={"registered-worker"},
    )
    _check("nonexistent target fails check 1", not ok and "check 1 failed" in error, error)
    _check("failure names missing target", "codex-1" in error, error)
    _check("failure includes live target list", "Live targets observed:" in error, error)
    _check(
        "unregistered target gives provision/register remedy",
        "Remedy: target is not registered and no reader is visible" in error
        and "provision/start/register" in error
        and "where policy explicitly permits it" in error,
        error,
    )

    ok, error = validate_target_reader(
        redis_client,
        "blocked-gemini",
        "check",
        tmux_sessions={"blocked-gemini"},
        registered_sessions={"blocked-gemini"},
    )
    _check("existing readerless backlog fails check 2", not ok and "check 2 failed" in error, error)
    _check(
        "registered non-draining target gives retry remedy",
        "Remedy: target is registered but its inbox is not draining" in error
        and "retry in a few seconds" in error
        and "Do not use --allow-unregistered-target to bypass a busy reader" in error
        and "intentional pre-provisioning" not in error,
        error,
    )

    ok, error = validate_target_reader(
        redis_client,
        "registered-worker",
        "check",
        tmux_sessions=set(),
        registered_sessions={"registered-worker"},
    )
    _check("registered name without reader evidence fails check 3", not ok and "check 3 failed" in error, error)
    _check(
        "registered name without reader evidence gives repair/wait remedy",
        "Remedy: target is registered but no live reader is visible" in error
        and "start or repair the reader" in error
        and "wait until it reports fresh activity" in error
        and "--allow-unregistered-target" not in error,
        error,
    )

    ok, error = validate_target_reader(
        redis_client,
        "stale-seat",
        "check",
        tmux_sessions={"stale-seat"},
        registered_sessions=set(),
    )
    _check("stale session fails check 3", not ok and "check 3 failed" in error, error)

    ok, error = validate_target_reader(
        redis_client,
        "depth-error",
        "check",
        tmux_sessions={"depth-error"},
        registered_sessions=set(),
    )
    _check(
        "unreadable inbox depth fails check 2",
        not ok and "check 2 failed" in error and "simulated LLEN failure" in error,
        error,
    )

    ok, error = validate_target_reader(
        redis_client,
        "activity-error",
        "check",
        tmux_sessions={"activity-error"},
        registered_sessions=set(),
    )
    _check(
        "unreadable last_activity fails check 3",
        not ok and "check 3 failed" in error and "simulated GET failure" in error,
        error,
    )

    ok, _ = validate_target_reader(
        redis_client,
        "taey",
        "check",
        tmux_sessions=set(),
        registered_sessions={"taey"},
    )
    _check("canonical Taey active lease passes despite stale last_activity", ok, "taey")

    snapshot = target_liveness_snapshot(
        redis_client,
        "check",
        tmux_sessions=set(),
        registered_sessions={"taey"},
    )
    _check(
        "canonical Taey pass exposes active-reader diagnostic",
        "taey" in snapshot["active_reader"],
        snapshot,
    )
    _check(
        "canonical Taey pass exposes unexpired lease count",
        snapshot["unexpired_active_turns"].get("taey") == 1,
        snapshot,
    )

    ok, error = validate_target_reader(
        redis_client,
        "taey-council-2",
        "check",
        tmux_sessions=set(),
        registered_sessions=set(),
    )
    _check(
        "canonical Taey expired lease fails check 3",
        not ok and "check 3 failed" in error,
        error,
    )
    _check(
        "expired lease diagnostic reports zero unexpired turns",
        "unexpired_active_turns=0" in error,
        error,
    )

    # PR98 regression: a canonical Taey line reader with turns_open=1 and only an
    # expired active turn must fail closed even when the local tmux probe is unknown.
    # The generic tmux_probe_error fallback must never re-admit a Taey line reader
    # after the authoritative active-reader predicate already failed on
    # unexpired_active_turns=0 (fallback guarded with `not is_taey_line_reader`).
    with patch(
        "notifications.targets.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["tmux", "list-sessions"], timeout=2),
    ):
        ok, error = validate_target_reader(
            redis_client,
            "taey-council-2",
            "check",
            tmux_sessions=None,
            registered_sessions=set(),
        )
        snapshot = target_liveness_snapshot(
            redis_client,
            "check",
            tmux_sessions=None,
            registered_sessions=set(),
        )
    _check(
        "canonical Taey expired lease fails check 3 even when tmux probe is unknown",
        not ok and "check 3 failed" in error,
        error,
    )
    _check(
        "canonical Taey expired lease not admitted as active_reader under unknown probe",
        "taey-council-2" not in snapshot["active_reader"],
        snapshot["active_reader"],
    )
    _check(
        "canonical Taey expired lease not admitted via probe_unknown_allowed under unknown probe",
        "taey-council-2" not in snapshot["probe_unknown_allowed"],
        snapshot["probe_unknown_allowed"],
    )

    ok, error = validate_target_reader(
        redis_client,
        "taey-council-3",
        "check",
        tmux_sessions=set(),
        registered_sessions=set(),
    )
    _check(
        "malformed active-turn lease fails closed",
        not ok and "check 3 failed" in error and "invalid lease score" in error,
        error,
    )

    ok, error = validate_target_reader(
        redis_client,
        "taey-council-4",
        "check",
        tmux_sessions=set(),
        registered_sessions=set(),
    )
    _check(
        "unreadable active-turn lease fails closed",
        not ok and "check 3 failed" in error and "simulated ZRANGEBYSCORE failure" in error,
        error,
    )

    ok, error = validate_target_reader(
        redis_client,
        "lookalike-active",
        "check",
        tmux_sessions={"lookalike-active"},
        registered_sessions=set(),
    )
    _check(
        "non-Taey active-turn key cannot bypass stale activity",
        not ok and "check 3 failed" in error,
        error,
    )

    ok, error = validate_target_reader(
        redis_client,
        "taey-council-5",
        "check",
        tmux_sessions=set(),
        registered_sessions=set(),
    )
    _check(
        "canonical Taey live lease cannot bypass non-draining inbox",
        not ok and "check 2 failed" in error,
        error,
    )

    queued_taey = FakeRedis(
        values={
            "check:taey:last_activity": str(now - 86400),
            "check:taey:turns_open": "1",
        },
        lengths={"check:taey:inbox": 3},
        trace_entries=[],
        sorted_sets={"check:taey:active_turns": [("turn-live", now + 120)]},
    )
    receipt = {
        "display": ":4",
        "extraction_status": "succeeded",
        "monitor_id": "monitor-gemini-r1",
        "platform": "gemini",
        "schema": "taey.consult_terminal_receipt.v1",
        "terminal": True,
    }
    ok, _ = validate_target_reader(
        queued_taey,
        "taey",
        "check",
        tmux_sessions=set(),
        registered_sessions={"taey"},
        from_node="consult-monitor",
        msg_type="result",
        body=json.dumps(receipt),
    )
    _check(
        "exact consult terminal receipt queues behind active Taey turn",
        ok,
        receipt,
    )

    failed_receipt = {
        **receipt,
        "error": "mapped extraction failure",
        "extraction_status": "failed",
    }
    ok, _ = validate_target_reader(
        queued_taey,
        "taey",
        "check",
        tmux_sessions=set(),
        registered_sessions={"taey"},
        from_node="consult-monitor",
        msg_type="result",
        body=json.dumps(failed_receipt),
    )
    _check(
        "exact failed consult receipt queues behind active Taey turn",
        ok,
        failed_receipt,
    )

    inactive_taey = FakeRedis(
        values={
            "check:taey:last_activity": str(now - 86400),
            "check:taey:turns_open": "0",
        },
        lengths={"check:taey:inbox": 3},
        trace_entries=[],
    )
    ok, error = validate_target_reader(
        inactive_taey,
        "taey",
        "check",
        tmux_sessions=set(),
        registered_sessions={"taey"},
        from_node="consult-monitor",
        msg_type="result",
        body=json.dumps(receipt),
    )
    _check(
        "consult receipt cannot bypass without an active Taey turn",
        not ok and "check 2 failed" in error,
        error,
    )

    malformed_receipts = (
        ("wrong sender", "other-monitor", "result", json.dumps(receipt)),
        ("wrong type", "consult-monitor", "status", json.dumps(receipt)),
        ("invalid json", "consult-monitor", "result", "{not-json"),
        (
            "wrong schema",
            "consult-monitor",
            "result",
            json.dumps({**receipt, "schema": "other.schema"}),
        ),
        (
            "non-terminal",
            "consult-monitor",
            "result",
            json.dumps({**receipt, "terminal": False}),
        ),
        (
            "unsupported extraction status",
            "consult-monitor",
            "result",
            json.dumps({**receipt, "extraction_status": "pending"}),
        ),
        (
            "missing identity",
            "consult-monitor",
            "result",
            json.dumps({**receipt, "monitor_id": ""}),
        ),
    )
    for name, from_node, msg_type, body in malformed_receipts:
        ok, error = validate_target_reader(
            queued_taey,
            "taey",
            "check",
            tmux_sessions=set(),
            registered_sessions={"taey"},
            from_node=from_node,
            msg_type=msg_type,
            body=body,
        )
        _check(
            f"{name} cannot bypass non-draining inbox",
            not ok and "check 2 failed" in error,
            error,
        )

    ok, error = validate_target_reader(
        queued_taey,
        "taey",
        "check",
        tmux_sessions=set(),
        registered_sessions={"taey"},
        from_node="consult-monitor",
        msg_type="result",
        body=json.dumps(receipt),
        explicit_handoff=True,
    )
    _check(
        "explicit handoff cannot use the record-only exception",
        not ok and "check 2 failed" in error,
        error,
    )

    ok, error = validate_target_reader(
        redis_client,
        "taey-council-5",
        "check",
        tmux_sessions=set(),
        registered_sessions=set(),
        from_node="consult-monitor",
        msg_type="result",
        body=json.dumps(receipt),
    )
    _check(
        "consult receipt cannot bypass a council inbox",
        not ok and "check 2 failed" in error,
        error,
    )

    ok, _ = validate_target_reader(
        redis_client,
        "taey-council-1",
        "check",
        tmux_sessions=set(),
        registered_sessions=set(),
    )
    _check("canonical council line reader passes without tmux", ok, "taey-council-1")

    ok, _ = validate_target_reader(
        redis_client,
        "stale-idle",
        "check",
        tmux_sessions={"stale-idle"},
        registered_sessions=set(),
    )
    _check("idle drained reader passes despite stale last_activity", ok, "stale-idle")

    with patch(
        "notifications.targets.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["tmux", "list-sessions"], timeout=2),
    ):
        ok, _ = validate_target_reader(
            redis_client,
            "registered-timeout",
            "check",
            tmux_sessions=None,
            registered_sessions={"registered-timeout"},
        )
    _check("registered target passes when local tmux probe is unknown", ok, "registered-timeout")

    ok, _ = validate_target_reader(
        redis_client,
        "live-empty",
        "check",
        tmux_sessions={"live-empty"},
        registered_sessions=set(),
    )
    _check("live empty reader passes", ok, "live-empty")

    ok, _ = validate_target_reader(
        redis_client,
        "draining-seat",
        "check",
        tmux_sessions={"draining-seat"},
        registered_sessions=set(),
    )
    _check("non-empty but visibly draining reader passes", ok, "draining-seat")

    ok, error = validate_target_reader(
        redis_client,
        "dead-headless",
        "check",
        tmux_sessions=set(),
        registered_sessions=set(),
    )
    _check("dead headless with piling inbox fails", not ok and "check 2 failed" in error, error)

    ok, error = validate_target_reader(
        redis_client,
        "seat-only",
        "check",
        tmux_sessions=set(),
        registered_sessions=set(),
    )
    _check("seat registration without fresh drain evidence fails", not ok and "check 3 failed" in error, error)

    ok, error = validate_target_reader(
        redis_client,
        "idle-piling",
        "check",
        tmux_sessions=set(),
        registered_sessions={"idle-piling"},
    )
    _check("idle registered seat with piling inbox fails", not ok and "check 2 failed" in error, error)

    with patch(
        "notifications.targets.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["tmux", "list-sessions"], timeout=2),
    ):
        ok, error = validate_target_reader(
            redis_client,
            "nonexistent-timeout",
            "check",
            tmux_sessions=None,
            registered_sessions={"registered-timeout"},
        )
    _check("nonexistent target fails check 1 when tmux probe is unknown", not ok and "check 1 failed" in error, error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
