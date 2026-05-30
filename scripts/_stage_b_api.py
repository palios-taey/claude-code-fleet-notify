#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API_BASE = os.environ.get("ORCH_API_BASE", "http://127.0.0.1:5002")


def current_session() -> str:
    env_session = os.environ.get("TAEY_NODE_ID")
    if env_session:
        return env_session
    result = subprocess.run(
        ["tmux", "display-message", "-p", "#S"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise SystemExit("Unable to detect current tmux session")
    return result.stdout.strip()


def api_call(method: str, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {body}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Request failed: {exc}")


def resolve_condition(project_payload: dict, selector: str) -> dict:
    project = project_payload.get("project", project_payload)
    conditions = [
        cond for cond in project.get("user_stop_conditions", [])
        if not cond.get("deprecated_at")
    ]
    matches = [
        cond for cond in conditions
        if cond.get("id") == selector
        or str(cond.get("id", "")).startswith(selector)
        or cond.get("label") == selector
        or str(cond.get("label", "")).startswith(selector)
    ]
    if not matches:
        raise SystemExit(f"No active condition matches '{selector}'")
    if len(matches) > 1:
        labels = ", ".join(f"{cond.get('label')} [{cond.get('id')}]" for cond in matches)
        raise SystemExit(f"Condition selector '{selector}' is ambiguous: {labels}")
    return matches[0]


def print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def add_session_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", default=None, help="Override detected tmux session")
