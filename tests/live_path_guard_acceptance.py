#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import _shared as shared  # noqa: E402
import pre_tool_live_guard as hook  # noqa: E402
import notifications.targets as targets  # noqa: E402


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, int | None]] = {}
        self.pushes: list[tuple[str, dict]] = []

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = (value, ex)
        return True

    def delete(self, key: str) -> None:
        self.values.pop(key, None)

    def lpush(self, key: str, value: str) -> None:
        self.pushes.append((key, json.loads(value)))

    def xadd(self, *_args, **_kwargs) -> str:
        return "1-0"


def require(name: str, condition: bool, detail) -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"PASS: {name}")


def decision(cwd: Path, command: str) -> tuple[bool, str]:
    return shared.live_guard_decision(str(cwd), "Bash", {"command": command})


def main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        live = root / "live"
        worktree = root / ".peer-worktrees" / "seat"
        live.mkdir()
        worktree.mkdir(parents=True)
        registry = root / "registry.json"
        registry.write_text(json.dumps({
            "live_checkout_paths": [str(live)],
            "worktree_roots": [str(root / ".peer-worktrees")],
            "live_db_endpoints": [
                {"kind": "neo4j", "host": "127.0.0.1", "port": 7689},
                {"kind": "redis", "host": "127.0.0.1", "port": 6379},
            ],
        }))

        with mock.patch.dict(os.environ, {"CF_LIVE_PATH_REGISTRY": str(registry)}):
            allowed, reason = decision(live, "git status")
            require("valid registry preserves read-only call", allowed and not reason, (allowed, reason))
            allowed, reason = decision(live, "git commit -m x")
            require("valid registry denies live mutation", not allowed and reason.startswith("BLOCKED:"), (allowed, reason))
            allowed, reason = decision(worktree, "git commit -m x")
            require("worktree-relative mutation remains allowed", allowed and not reason, (allowed, reason))
            allowed, reason = decision(worktree, f"rm -rf {live}/sub")
            require("worktree cannot target live checkout explicitly", not allowed and str(live) in reason, (allowed, reason))
            allowed, reason = decision(worktree, f"cat x | rm -rf {live}/sub")
            require("pipeline cannot hide live target", not allowed and str(live) in reason, (allowed, reason))
            allowed, reason = decision(live, "git status '")
            require("unparseable command cannot authorize mutation", not allowed and "could not prove" in reason, (allowed, reason))

        committed_registry = root / "home" / "the-conductor" / "config" / "live_path_registry.json"
        committed_registry.parent.mkdir(parents=True)
        committed_registry.write_text(registry.read_text())
        with mock.patch.dict(os.environ, {"HOME": str(root / "home")}, clear=False):
            os.environ.pop("CF_LIVE_PATH_REGISTRY", None)
            os.environ.pop("ORCH_LIVE_PATH_REGISTRY", None)
            require(
                "env-less process discovers committed registry",
                shared._live_guard_registry_path() == str(committed_registry),
                shared._live_guard_registry_path(),
            )

        missing = root / "missing.json"
        with mock.patch.dict(os.environ, {"CF_LIVE_PATH_REGISTRY": str(missing)}):
            allowed, reason = decision(live, "git status")
            require("missing registry preserves read", allowed and "registry file absent" in reason, (allowed, reason))
            allowed, reason = decision(live, "git commit -m x")
            require("missing registry fails closed for mutation", not allowed and "registry file absent" in reason, (allowed, reason))
            allowed, reason = decision(live, "git branch new-work")
            require("missing registry cannot authorize git ref mutation", not allowed and "registry file absent" in reason, (allowed, reason))

        invalid = root / "invalid.json"
        invalid.write_text("{}")
        with mock.patch.dict(os.environ, {"CF_LIVE_PATH_REGISTRY": str(invalid)}):
            allowed, reason = decision(live, "git status")
            require("invalid registry preserves read", allowed and "is invalid" in reason, (allowed, reason))
            allowed, reason = decision(live, "rm -rf x")
            require("invalid registry fails closed for mutation", not allowed and "is invalid" in reason, (allowed, reason))

        with mock.patch.object(shared, "_live_guard_load_registry", side_effect=RuntimeError("injected")):
            allowed, reason = decision(live, "git status")
            require("internal failure preserves read", allowed and "internal error" in reason, (allowed, reason))
            allowed, reason = decision(live, "git commit -m x")
            require("internal failure fails closed for mutation", not allowed and "internal error" in reason, (allowed, reason))

    fake_redis = FakeRedis()
    defect = "LIVE-PATH GUARD DEFECT: registry file absent at /registry"
    with mock.patch.object(hook, "get_redis_and_node", return_value=(fake_redis, "worker-codex")), mock.patch.object(
        targets, "resolve_supervisor", return_value="owner-codex"
    ):
        hook._route_defect_once(defect)
        hook._route_defect_once(defect)
    require("defect route is deduped", len(fake_redis.pushes) == 1, fake_redis.pushes)
    queue, message = fake_redis.pushes[0]
    require("defect routes to owner", queue == "taey:owner-codex:inbox", queue)
    require("defect route has bounded TTL", any(value[1] == 900 for value in fake_redis.values.values()), fake_redis.values)
    require("defect route is typed", message.get("type") == "defect", message)

    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "cwd": "/tmp",
        "tool_input": {"command": "git status"},
    }
    output = io.StringIO()
    with mock.patch.object(hook, "read_stdin_json", return_value=payload), mock.patch.object(
        hook, "live_guard_decision", return_value=(True, defect)
    ), mock.patch.object(hook, "_route_defect_once"), contextlib.redirect_stdout(output):
        hook.main()
    require("read-only passenger receives no repeated warning", json.loads(output.getvalue()) == {}, output.getvalue())

    output = io.StringIO()
    with mock.patch.object(hook, "read_stdin_json", return_value=payload), mock.patch.object(
        hook, "live_guard_decision", side_effect=RuntimeError("injected")
    ), mock.patch.object(hook, "live_guard_mutation_intent", return_value="mutating"), mock.patch.object(
        hook, "_route_defect_once"
    ), contextlib.redirect_stdout(output):
        hook.main()
    wrapper = json.loads(output.getvalue())["hookSpecificOutput"]
    require("wrapper failure denies mutation", wrapper.get("permissionDecision") == "deny", wrapper)


if __name__ == "__main__":
    main()
