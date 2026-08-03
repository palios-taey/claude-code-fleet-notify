#!/usr/bin/env python3
from __future__ import annotations

import time
import os
import subprocess
import sys
from fnmatch import fnmatch
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from notifications.targets import validate_target_reader


class FakeRedis:
    def __init__(self, values: dict[str, object], lengths: dict[str, object], trace_entries: list[tuple[str, dict[str, str]]]):
        self._values = dict(values)
        self._lengths = dict(lengths)
        self._trace_entries = list(trace_entries)

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
            "check:taey:last_activity": str(now),
            "check:stale-idle:last_activity": str(now - 86400),
            "check:stale-idle:idle": "1",
            "check:stale-idle:turns_open": "0",
            "check:taey-council-1:last_activity": str(now - 86400),
            "check:taey-council-1:idle": "1",
            "check:taey-council-1:turns_open": "0",
            "check:taey-council-1:seat_registration": "{}",
            "check:taey-council-1:last_drain_at": str(now),
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
        },
        trace_entries=[
            ("1-0", {"ev": "drain", "node": "draining-seat", "wall": str(now)}),
        ],
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

    ok, error = validate_target_reader(
        redis_client,
        "blocked-gemini",
        "check",
        tmux_sessions={"blocked-gemini"},
        registered_sessions=set(),
    )
    _check("existing readerless backlog fails check 2", not ok and "check 2 failed" in error, error)

    ok, error = validate_target_reader(
        redis_client,
        "registered-worker",
        "check",
        tmux_sessions=set(),
        registered_sessions={"registered-worker"},
    )
    _check("registered name without reader evidence fails check 3", not ok and "check 3 failed" in error, error)

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
    _check("registered headless taey passes without tmux", ok, "taey")

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
