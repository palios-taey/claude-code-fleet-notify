from __future__ import annotations

import unittest
from unittest import mock

from notifications import task_liveness


class TaskLivenessTests(unittest.TestCase):
    def test_in_progress_task_allows_peer_idle(self):
        with mock.patch.object(task_liveness, "_api_json", return_value={"status": "in_progress"}):
            allowed, reason, task = task_liveness.peer_idle_allowed("task-live", "worker-codex", "conductor")

        self.assertTrue(allowed)
        self.assertEqual("in_progress", reason)
        self.assertEqual({"status": "in_progress"}, task)

    def test_terminal_task_suppresses_peer_idle(self):
        with mock.patch.object(task_liveness, "_api_json", return_value={"status": "failed"}):
            allowed, reason, task = task_liveness.peer_idle_allowed("task-failed", "worker-codex", "conductor")

        self.assertFalse(allowed)
        self.assertEqual("task_status_failed", reason)
        self.assertEqual({"status": "failed"}, task)

    def test_orphan_task_suppresses_peer_idle(self):
        with mock.patch.object(task_liveness, "_api_json", side_effect=RuntimeError("missing")):
            allowed, reason, task = task_liveness.peer_idle_allowed("task-orphan", "worker-codex", "conductor")

        self.assertFalse(allowed)
        self.assertEqual("task_unresolved", reason)
        self.assertIsNone(task)

    def test_self_supervisor_suppresses_peer_idle_without_api_call(self):
        with mock.patch.object(task_liveness, "_api_json") as api_json:
            allowed, reason, task = task_liveness.peer_idle_allowed("task-live", "worker-codex", "worker-codex")

        self.assertFalse(allowed)
        self.assertEqual("self_supervisor", reason)
        self.assertIsNone(task)
        api_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
