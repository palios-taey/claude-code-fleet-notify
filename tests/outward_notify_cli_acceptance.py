#!/usr/bin/env python3
"""Acceptance: taey-notify gates worker outward enqueue before Redis lpush.

Uses fake redis and a fake orchestrator authorization module; no live Redis,
Neo4j, tmux, or GitHub.
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import sys
import time
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "taey-notify"
PREFIX = "taey-test"
WORKER = "worker-grok"
TARGET = "supervisor-codex"
CURRENT_KEY = f"{PREFIX}:{WORKER}:current_task"
INBOX_KEY = f"{PREFIX}:{TARGET}:inbox"


FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {detail}"))
    if not condition:
        FAILURES.append(label)


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.lpushes: list[tuple[str, str]] = []
        self.traces: list[tuple[str, dict[str, str]]] = []

    def ping(self) -> bool:
        return True

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def delete(self, key: str) -> None:
        self.store.pop(key, None)

    def lpush(self, key: str, value: str) -> int:
        self.lpushes.append((key, value))
        return len(self.lpushes)

    def xadd(self, key: str, values: dict[str, str], **_kwargs) -> str:
        self.traces.append((key, dict(values)))
        return f"{len(self.traces)}-0"


class FakeOutwardAuthorizationError(RuntimeError):
    pass


def _install_fake_modules(redis_client: FakeRedis) -> dict[str, object]:
    redis_module = types.SimpleNamespace(
        Redis=lambda **_kwargs: redis_client,
    )
    outward_module = types.ModuleType("fleet_orchestrator.outward_capability")
    outward_module.OutwardAuthorizationError = FakeOutwardAuthorizationError

    def require_outward_capability(session_id: str, *, channel: str = "", redis_client=None, **_kwargs):
        if channel != "taey_notify":
            raise FakeOutwardAuthorizationError(f"unexpected channel {channel!r}")
        r = redis_client
        current = json.loads(r.get(f"{PREFIX}:{session_id}:current_task") or "null")
        if not current:
            raise FakeOutwardAuthorizationError(
                f"no live current_task binding for session {session_id}; mutation denied after unbind/revocation"
            )
        return {"allowed": True}

    outward_module.require_outward_capability = require_outward_capability
    package = types.ModuleType("fleet_orchestrator")
    return {
        "redis": redis_module,
        "fleet_orchestrator": package,
        "fleet_orchestrator.outward_capability": outward_module,
    }


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader("taey_notify_cli_acceptance_subject", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_cli(module, redis_client: FakeRedis, *args: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    argv = ["taey-notify", *args, "--key-prefix", PREFIX, "--allow-unregistered-target"]
    with mock.patch.dict(sys.modules, _install_fake_modules(redis_client)), \
         mock.patch.object(sys, "argv", argv), \
         contextlib.redirect_stdout(stdout), \
         contextlib.redirect_stderr(stderr):
        try:
            result = module.main()
            code = 0 if result is None else int(result)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, stdout.getvalue(), stderr.getvalue()


def main() -> int:
    redis_client = FakeRedis()
    module = _load_cli_module()

    code, _stdout, stderr = _run_cli(
        module,
        redis_client,
        TARGET,
        "RESPONSE_READY: stale",
        "--type",
        "response_ready",
        "--from",
        WORKER,
    )
    _check("unbound response_ready denied", code == 1 and "SAFETY DENY" in stderr, stderr)
    _check("unbound response_ready did not lpush", not redis_client.lpushes, redis_client.lpushes)

    redis_client.set(
        CURRENT_KEY,
        json.dumps(
            {
                "task_id": "task-f396305d-fixture",
                "supervisor": TARGET,
                "started_at": time.time(),
            }
        ),
    )
    code, stdout, stderr = _run_cli(
        module,
        redis_client,
        TARGET,
        "RESPONSE_READY: bound",
        "--type",
        "response_ready",
        "--from",
        WORKER,
    )
    _check("bound response_ready allowed", code == 0 and "OK: sent" in stdout, (code, stdout, stderr))
    _check("bound response_ready enqueued once", len(redis_client.lpushes) == 1, redis_client.lpushes)
    _check("bound response_ready target inbox", redis_client.lpushes[-1][0] == INBOX_KEY, redis_client.lpushes)

    redis_client.delete(CURRENT_KEY)
    code, stdout, stderr = _run_cli(
        module,
        redis_client,
        TARGET,
        "plain operator message",
        "--type",
        "message",
        "--from",
        WORKER,
    )
    _check("plain message skips outward gate", code == 0 and "OK: sent" in stdout, (code, stdout, stderr))
    _check("plain message enqueued after skip", len(redis_client.lpushes) == 2, redis_client.lpushes)

    if FAILURES:
        print(f"FAIL: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("PASS outward_notify_cli_acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
