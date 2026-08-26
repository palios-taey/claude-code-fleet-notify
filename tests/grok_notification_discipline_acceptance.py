#!/usr/bin/env python3
import importlib.machinery
import os
import pathlib
import subprocess
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
NOTIFY = importlib.machinery.SourceFileLoader(
    "taey_notify_grok_discipline",
    str(ROOT / "scripts" / "taey-notify"),
).load_module()


def check(label, condition, actual):
    if not condition:
        raise AssertionError(f"FAIL: {label}: {actual!r}")
    print(f"PASS: {label}")


with mock.patch.dict(os.environ, {}, clear=False):
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_PANE", None)
    with mock.patch.object(NOTIFY.os, "getppid", return_value=1):
        with mock.patch.object(NOTIFY.subprocess, "run") as run:
            principal = NOTIFY.detect_execution_principal()
    check("headless caller does not consult arbitrary tmux client", not run.called, run.call_args)
    check("headless non-Grok caller is not classified as Grok", principal == "", principal)


tmux_result = subprocess.CompletedProcess(
    args=[], returncode=0, stdout="infra-grok\n", stderr=""
)
with mock.patch.dict(
    os.environ,
    {"TMUX": "/tmp/tmux-1000/default,1,0", "TMUX_PANE": "%42"},
    clear=False,
):
    with mock.patch.object(NOTIFY.subprocess, "run", return_value=tmux_result) as run:
        principal = NOTIFY.detect_execution_principal()
    check("Grok caller resolves from its exact tmux pane", principal == "infra-grok", principal)
    check(
        "tmux lookup is pane-bound",
        run.call_args.args[0] == ["tmux", "display-message", "-p", "-t", "%42", "#S"],
        run.call_args,
    )
