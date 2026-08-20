#!/usr/bin/env python3
"""Acceptance: opt-in *-codex supervisor topology vs legacy suffix-strip.

Does not touch live Redis. Production-shaped parent maps are fixtures taken
from the 2026-08-19 Mira inventory; the live notify-router.env still leaves
NOTIFY_SUPERVISOR_IDS unset.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from notifications.targets import (
    NOTIFY_SUPERVISOR_IDS_ENV,
    SupervisorTopologyError,
    default_notify_target,
    load_supervisor_topology,
    resolve_supervisor,
)


class FakeRedis:
    def __init__(self, parents: dict[str, str], prefix: str = "taey"):
        self._values = {f"{prefix}:{node}:parent": value for node, value in parents.items()}

    def get(self, key: str):
        return self._values.get(key)


# Observed 2026-08-19 production parent keys (subset). Not live reads.
PRODUCTION_PARENTS = {
    "conductor": "conductor",
    "conductor-codex": "conductor",
    "conductor-gemini": "conductor",
    "conductor-grok": "conductor-grok",
    "infra-codex": "infra",
    "infra-gemini": "infra-codex",
    "infra-grok": "infra-grok",
    "hunter-codex": "hunter",
    "linkedin-codex": "treasurer",
}

CURRENT_ORCH_SESSION_IDS = (
    "conductor,weaver,tutor,infra,taeys-hands,treasurer,hunter,taey,"
    "taey-ed,x-claude,linkedin,job-seeker,jd-reader,upwork,taey-ed-operator"
)


def _check(name: str, condition: bool, detail: object) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {name}: {detail}")
    print(f"PASS: {name}")


def _resolve(node_id: str, parents: dict[str, str] | None = None) -> str | None:
    return resolve_supervisor(FakeRedis(parents or {}), node_id)


def main() -> int:
    production_env = {
        key: value
        for key, value in os.environ.items()
        if key != NOTIFY_SUPERVISOR_IDS_ENV
    }

    with patch.dict(os.environ, production_env, clear=True):
        _check("unset env loads no topology", load_supervisor_topology() is None, load_supervisor_topology())
        _check(
            "legacy conductor-codex strips to Claude",
            _resolve("conductor-codex", PRODUCTION_PARENTS) == "conductor",
            _resolve("conductor-codex", PRODUCTION_PARENTS),
        )
        _check(
            "legacy conductor-gemini strips to Claude",
            _resolve("conductor-gemini", PRODUCTION_PARENTS) == "conductor",
            _resolve("conductor-gemini", PRODUCTION_PARENTS),
        )
        _check(
            "legacy conductor-grok self-parent still suffix-strips",
            _resolve("conductor-grok", PRODUCTION_PARENTS) == "conductor",
            _resolve("conductor-grok", PRODUCTION_PARENTS),
        )
        _check(
            "legacy unsuffixed Claude with self-parent returns self",
            _resolve("conductor", PRODUCTION_PARENTS) == "conductor",
            _resolve("conductor", PRODUCTION_PARENTS),
        )
        _check(
            "legacy unsuffixed Claude with no parent is top-level",
            _resolve("weaver", {}) is None,
            _resolve("weaver", {}),
        )
        _check(
            "legacy default target from conductor-codex is Claude",
            default_notify_target(FakeRedis(PRODUCTION_PARENTS), "conductor-codex") == "conductor",
            default_notify_target(FakeRedis(PRODUCTION_PARENTS), "conductor-codex"),
        )

    with patch.dict(os.environ, {**production_env, NOTIFY_SUPERVISOR_IDS_ENV: ""}, clear=True):
        _check("blank env keeps legacy", load_supervisor_topology() is None, load_supervisor_topology())
        _check(
            "blank env conductor-codex still strips",
            _resolve("conductor-codex", PRODUCTION_PARENTS) == "conductor",
            _resolve("conductor-codex", PRODUCTION_PARENTS),
        )

    opted = {**production_env, NOTIFY_SUPERVISOR_IDS_ENV: "conductor-codex,infra-codex"}
    with patch.dict(os.environ, opted, clear=True):
        topology = load_supervisor_topology()
        _check("opt-in loads topology", topology is not None, topology)
        assert topology is not None
        _check(
            "opt-in supervisors are exactly the configured *-codex names",
            topology.supervisors == frozenset({"conductor-codex", "infra-codex"}),
            topology.supervisors,
        )
        _check(
            "configured conductor-codex is top-level despite Redis parent=conductor",
            _resolve("conductor-codex", PRODUCTION_PARENTS) is None,
            _resolve("conductor-codex", PRODUCTION_PARENTS),
        )
        _check(
            "base Claude conductor resolves to conductor-codex",
            _resolve("conductor", PRODUCTION_PARENTS) == "conductor-codex",
            _resolve("conductor", PRODUCTION_PARENTS),
        )
        _check(
            "conductor-gemini resolves to conductor-codex not Claude",
            _resolve("conductor-gemini", PRODUCTION_PARENTS) == "conductor-codex",
            _resolve("conductor-gemini", PRODUCTION_PARENTS),
        )
        _check(
            "conductor-grok self-parent resolves to conductor-codex",
            _resolve("conductor-grok", PRODUCTION_PARENTS) == "conductor-codex",
            _resolve("conductor-grok", PRODUCTION_PARENTS),
        )
        _check(
            "infra-gemini stale Redis parent=infra-codex stays infra-codex",
            _resolve("infra-gemini", PRODUCTION_PARENTS) == "infra-codex",
            _resolve("infra-gemini", PRODUCTION_PARENTS),
        )
        _check(
            "unlisted hunter-codex keeps legacy strip to hunter",
            _resolve("hunter-codex", PRODUCTION_PARENTS) == "hunter",
            _resolve("hunter-codex", PRODUCTION_PARENTS),
        )
        _check(
            "unlisted linkedin-codex keeps Redis parent=treasurer",
            _resolve("linkedin-codex", PRODUCTION_PARENTS) == "treasurer",
            _resolve("linkedin-codex", PRODUCTION_PARENTS),
        )
        _check(
            "default notify from conductor-codex is None (top-level)",
            default_notify_target(FakeRedis(PRODUCTION_PARENTS), "conductor-codex") is None,
            default_notify_target(FakeRedis(PRODUCTION_PARENTS), "conductor-codex"),
        )
        _check(
            "default notify from conductor goes to conductor-codex",
            default_notify_target(FakeRedis(PRODUCTION_PARENTS), "conductor") == "conductor-codex",
            default_notify_target(FakeRedis(PRODUCTION_PARENTS), "conductor"),
        )
        _check(
            "explicit --target still wins",
            default_notify_target(FakeRedis(PRODUCTION_PARENTS), "conductor", "weaver") == "weaver",
            default_notify_target(FakeRedis(PRODUCTION_PARENTS), "conductor", "weaver"),
        )

    malformed_cases = {
        "current ORCH_SESSION_IDS Claude names": CURRENT_ORCH_SESSION_IDS,
        "empty token": "conductor-codex,",
        "duplicate": "conductor-codex,conductor-codex",
        "invalid chars": "conductor-codex!",
        "nested peer family": "conductor-codex-codex",
        "bare suffix": "-codex",
    }
    for name, raw in malformed_cases.items():
        raised = None
        with patch.dict(os.environ, {**production_env, NOTIFY_SUPERVISOR_IDS_ENV: raw}, clear=True):
            try:
                load_supervisor_topology()
            except SupervisorTopologyError as exc:
                raised = exc
        _check(f"malformed {name} fails loud", raised is not None, raised)

    with patch.dict(os.environ, {**production_env, NOTIFY_SUPERVISOR_IDS_ENV: "conductor"}, clear=True):
        from notifications.daemon import main as daemon_main

        raised_exit = None
        with patch.object(sys, "argv", ["notifications/daemon.py"]):
            try:
                daemon_main()
            except SystemExit as exc:
                raised_exit = exc
        _check("daemon start fails loud on malformed topology", raised_exit is not None, raised_exit)
        _check(
            "daemon error names NOTIFY_SUPERVISOR_IDS",
            raised_exit is not None and NOTIFY_SUPERVISOR_IDS_ENV in str(raised_exit),
            raised_exit,
        )

    print("PASS: supervisor topology acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
