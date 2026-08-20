#!/usr/bin/env python3
"""Isolated acceptance: unbind revokes worker outward taey-notify enqueue."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from notifications.outward_capability_gate import (  # noqa: E402
    OutwardNotifyDenied,
    is_control_plane_exception,
    require_outward_notify_capability,
)


FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {detail}"))
    if not condition:
        FAILURES.append(label)


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


def main() -> int:
    prefix = "taey-test"
    session = "conductor-grok"
    redis = FakeRedis()
    key = f"{prefix}:{session}:current_task"

    import types
    package = types.ModuleType("fleet_orchestrator")
    outward = types.ModuleType("fleet_orchestrator.outward_capability")

    class FakeOutwardAuthorizationError(RuntimeError):
        pass

    def require_outward_capability(session_id: str, **kwargs):
        client = kwargs.get("redis_client") or redis
        raw = client.get(f"{prefix}:{session_id}:current_task")
        if not raw:
            raise FakeOutwardAuthorizationError(
                f"no live current_task binding for session {session_id}"
            )
        return {"allowed": True}

    outward.OutwardAuthorizationError = FakeOutwardAuthorizationError
    outward.require_outward_capability = require_outward_capability
    sys.modules["fleet_orchestrator"] = package
    sys.modules["fleet_orchestrator.outward_capability"] = outward

    try:
        require_outward_notify_capability(session, "message", redis, key_prefix=prefix)
        _check("unbound worker message denied", False, "expected OutwardNotifyDenied")
    except OutwardNotifyDenied as exc:
        _check(
            "unbound worker message denied",
            "no live current_task binding" in str(exc)
            or "fleet_orchestrator outward capability required" in str(exc),
            exc,
        )

    redis.set(
        key,
        json.dumps(
            {
                "task_id": "task-f396305d-fixture",
                "supervisor": "taey-ed-codex",
                "started_at": time.time(),
            }
        ),
    )
    try:
        gate = require_outward_notify_capability(
            session, "response_ready", redis, key_prefix=prefix
        )
        _check("bound response_ready allowed via orch gate", gate == "orch", gate)
    except OutwardNotifyDenied as exc:
        _check(
            "bound response_ready allowed via orch gate",
            False,
            exc,
        )

    redis.delete(key)
    try:
        require_outward_notify_capability(
            session, "response_ready", redis, key_prefix=prefix
        )
        _check("unbound response_ready denied", False, "expected OutwardNotifyDenied")
    except OutwardNotifyDenied as exc:
        _check("unbound response_ready denied", "no live current_task binding" in str(exc), exc)

    import os
    from unittest import mock

    with mock.patch.dict(os.environ, {"NOTIFY_SUPERVISOR_IDS": "taey-ed-codex"}, clear=False):
        _check(
            "topology supervisor command is control-plane",
            is_control_plane_exception("taey-ed-codex", "command"),
        )
        _check(
            "topology supervisor unknown type is gated",
            not is_control_plane_exception("taey-ed-codex", "not-a-type"),
        )
        try:
            require_outward_notify_capability(
                "taey-ed-codex", "not-a-type", redis, key_prefix=prefix
            )
            _check("supervisor unknown type denied", False, "expected OutwardNotifyDenied")
        except OutwardNotifyDenied as exc:
            _check(
                "supervisor unknown type denied",
                "no live current_task binding" in str(exc),
                exc,
            )

    if FAILURES:
        print(f"FAIL: {FAILURES}")
        return 1
    print("PASS outward_notify_capability_acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
