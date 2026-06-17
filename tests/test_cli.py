from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


FAKE_REDIS = r'''
import json
import os


class Redis:
    def __init__(self, *args, **kwargs):
        self.path = os.environ["FAKE_REDIS_STATE"]

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        with open(self.path) as f:
            return json.load(f)

    def _save(self, data):
        with open(self.path, "w") as f:
            json.dump(data, f)

    def ping(self):
        return True

    def lpush(self, key, value):
        data = self._load()
        data.setdefault(key, [])
        data[key].insert(0, value)
        self._save(data)
        return len(data[key])

    def rpush(self, key, value):
        data = self._load()
        data.setdefault(key, [])
        data[key].append(value)
        self._save(data)
        return len(data[key])

    def rpop(self, key):
        data = self._load()
        values = data.get(key, [])
        value = values.pop() if values else None
        self._save(data)
        return value

    def lpop(self, key):
        data = self._load()
        values = data.get(key, [])
        value = values.pop(0) if values else None
        self._save(data)
        return value

    def lrange(self, key, start, end):
        values = list(self._load().get(key, []))
        length = len(values)
        if start < 0:
            start = max(length + start, 0)
        if end < 0:
            end = length + end
        return values[start:end + 1]

    def llen(self, key):
        return len(self._load().get(key, []))

    def set(self, key, value, ex=None):
        data = self._load()
        data[key] = value
        self._save(data)
        return True

    def sadd(self, key, *values):
        data = self._load()
        current = set(data.get(key, []))
        before = len(current)
        current.update(values)
        data[key] = sorted(current)
        self._save(data)
        return len(current) - before

    def get(self, key):
        return self._load().get(key)

    def delete(self, *keys):
        data = self._load()
        count = 0
        for key in keys:
            if key in data:
                del data[key]
                count += 1
        self._save(data)
        return count
'''


class CliTests(unittest.TestCase):
    def run_cli(self, args, env):
        return subprocess.run(
            [sys.executable, *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )

    def test_taey_notify_writes_expected_json_to_inbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_dir = Path(tmp)
            (fake_dir / "redis.py").write_text(FAKE_REDIS)
            state = fake_dir / "state.json"
            env = os.environ.copy()
            env.update({
                "PYTHONPATH": f"{fake_dir}:{ROOT}",
                "FAKE_REDIS_STATE": str(state),
                "TAEY_NODE_ID": "session-a",
            })

            self.run_cli(["scripts/taey-notify", "session-b", "hello"], env)

            data = json.loads(state.read_text())
            raw = data["taey:session-b:inbox"][0]
            msg = json.loads(raw)
            self.assertEqual("session-a", msg["from"])
            self.assertEqual("hello", msg["body"])
            self.assertEqual([], [key for key in data if ":handoff:" in key])

    def test_taey_notify_handoff_creates_scoped_record_with_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_dir = Path(tmp)
            (fake_dir / "redis.py").write_text(FAKE_REDIS)
            state = fake_dir / "state.json"
            env = os.environ.copy()
            env.update({
                "PYTHONPATH": f"{fake_dir}:{ROOT}",
                "FAKE_REDIS_STATE": str(state),
                "TAEY_NODE_ID": "conductor-codex",
                "CF_HANDOFF_ENFORCE": "1",
                "CF_HANDOFF_ENFORCE_SESSIONS": "conductor-codex",
            })

            self.run_cli(
                [
                    "scripts/taey-notify",
                    "worker-codex",
                    "please take this task",
                    "--handoff",
                    "--dispatcher-task-id",
                    "task-123",
                    "--actionable-inputs",
                    '{"packet_hash":"abc"}',
                ],
                env,
            )

            data = json.loads(state.read_text())
            inbox_raw = data["taey:worker-codex:inbox"][0]
            inbox_msg = json.loads(inbox_raw)
            self.assertEqual("explicit_handoff", inbox_msg["handoff_kind"])
            self.assertRegex(
                inbox_msg["msg_id"],
                r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            )
            record_key = f"taey:handoff:conductor-codex:{inbox_msg['msg_id']}"
            self.assertIn(record_key, data)
            record = json.loads(data[record_key])
            self.assertEqual("explicit_handoff", record["kind"])
            self.assertEqual("worker-codex", record["target_session_id"])
            self.assertEqual("task-123", record["dispatcher_task_id"])
            self.assertEqual(5, record["pickup_poll_budget"])
            self.assertEqual("queued", record["delivery_state"])
            self.assertIn("ack_backstop_at", record)
            self.assertEqual([inbox_msg["msg_id"]], data["taey:handoff-index:conductor-codex"])

    def test_taey_ack_peek_does_not_clear_and_ack_drains_with_pops(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_dir = Path(tmp)
            (fake_dir / "redis.py").write_text(FAKE_REDIS)
            state = fake_dir / "state.json"
            handoff_msg = {
                "from": "conductor-codex",
                "type": "command",
                "body": "handoff body",
                "msg_id": "123e4567-e89b-12d3-a456-426614174999",
                "handoff_kind": "explicit_handoff",
                "dispatcher_session_id": "conductor-codex",
                "target_session_id": "session-b",
                "message_hash": "hash-manual",
            }
            record_key = f"taey:handoff:conductor-codex:{handoff_msg['msg_id']}"
            ack_key = f"taey:handoff-ack:conductor-codex:session-b:{handoff_msg['msg_id']}"
            initial = {
                "taey:session-b:inbox": [
                    json.dumps({"from": "session-a", "type": "message", "body": "new"}),
                    json.dumps(handoff_msg),
                    json.dumps({"from": "session-a", "type": "message", "body": "old"}),
                ],
                record_key: json.dumps({
                    "kind": "explicit_handoff",
                    "dispatcher_session_id": "conductor-codex",
                    "target_session_id": "session-b",
                    "dispatcher_task_id": "task-manual",
                    "msg_id": handoff_msg["msg_id"],
                    "message_hash": "hash-manual",
                    "state": "pending_unacked",
                }),
            }
            state.write_text(json.dumps(initial))
            env = os.environ.copy()
            env.update({
                "PYTHONPATH": f"{fake_dir}:{ROOT}",
                "FAKE_REDIS_STATE": str(state),
                "TAEY_NODE_ID": "session-b",
            })

            peek = self.run_cli(["scripts/taey-ack", "--peek"], env)
            self.assertIn("PEEK MODE", peek.stdout)
            after_peek = json.loads(state.read_text())
            self.assertEqual(3, len(after_peek["taey:session-b:inbox"]))
            self.assertNotIn(ack_key, after_peek)

            ack = self.run_cli(["scripts/taey-ack"], env)
            self.assertIn("Drained all queues", ack.stdout)
            after_ack = json.loads(state.read_text())
            self.assertEqual([], after_ack["taey:session-b:inbox"])
            self.assertEqual(
                {"ack_by": "session-b", "message_hash": "hash-manual"},
                json.loads(after_ack[ack_key]),
            )
            record = json.loads(after_ack[record_key])
            self.assertEqual("receipt_acked", record["state"])
            self.assertEqual("message_pickup", record["receipt_source"])


if __name__ == "__main__":
    unittest.main()
