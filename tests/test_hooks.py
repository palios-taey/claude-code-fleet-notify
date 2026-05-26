from __future__ import annotations

import importlib
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from notifications.inbox import inbox_key, notifications_key, state_key
from tests.fakes import FakeRedis


class HookTestCase(unittest.TestCase):
    def run_hook(self, module_name, redis_client, stdin_text="{}"):
        module = importlib.import_module(module_name)
        with mock.patch("identity.detect_node_id", return_value="session-b"):
            with mock.patch("identity.redis_connect", return_value=redis_client):
                if hasattr(module, "detect_node_id"):
                    module.detect_node_id = lambda: "session-b"
                if hasattr(module, "redis_connect"):
                    module.redis_connect = lambda: redis_client
                with mock.patch.object(sys, "stdin", io.StringIO(stdin_text)):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        try:
                            module.main()
                        except SystemExit:
                            pass
                    return json.loads(out.getvalue() or "{}")


class StopHookTests(HookTestCase):
    def test_stop_sets_idle_deletes_tool_running_and_stamps_activity(self):
        r = FakeRedis()
        r.set(state_key("session-b", "tool_running"), "1")

        result = self.run_hook("hooks.stop_idle", r, "")

        self.assertEqual({}, result)
        self.assertEqual("1", r.store[state_key("session-b", "idle")])
        self.assertIn(state_key("session-b", "tool_running"), r.deleted)
        self.assertIn(state_key("session-b", "last_activity"), r.store)


class UserPromptSubmitHookTests(HookTestCase):
    def test_user_prompt_clears_idle_drains_and_returns_context(self):
        r = FakeRedis()
        r.set(state_key("session-b", "idle"), "1")
        r.lpush(inbox_key("session-b"), json.dumps({"from": "session-a", "type": "message", "body": "hello"}))

        result = self.run_hook("hooks.prompt_activity", r, "")

        self.assertIn(state_key("session-b", "idle"), r.deleted)
        self.assertEqual(0, r.llen(inbox_key("session-b")))
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("hello", context)


class PreToolUseHookTests(HookTestCase):
    def test_pre_tool_sets_tool_running_without_clearing_idle(self):
        r = FakeRedis()
        r.set(state_key("session-b", "idle"), "1")

        result = self.run_hook("hooks.pre_tool_activity", r, "{}")

        self.assertEqual("PreToolUse", result["hookSpecificOutput"]["hookEventName"])
        self.assertEqual("1", r.store[state_key("session-b", "tool_running")])
        self.assertEqual(60, r.expiry[state_key("session-b", "tool_running")])
        self.assertNotIn(state_key("session-b", "idle"), r.deleted)


class PostToolUseHookTests(HookTestCase):
    def test_post_tool_clears_tool_running_drains_all_queues_and_returns_context(self):
        r = FakeRedis()
        r.set(state_key("session-b", "tool_running"), "1")
        r.lpush(inbox_key("session-b"), json.dumps({"from": "session-a", "type": "message", "body": "inbox"}))
        r.rpush(notifications_key("session-b"), json.dumps({"from": "worker", "type": "notification", "body": "notif"}))

        result = self.run_hook("hooks.check_notifications", r, '{"tool_name":"Bash"}')

        self.assertIn(state_key("session-b", "tool_running"), r.deleted)
        self.assertEqual(0, r.llen(inbox_key("session-b")))
        self.assertEqual(0, r.llen(notifications_key("session-b")))
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("inbox", context)
        self.assertIn("notif", context)


if __name__ == "__main__":
    unittest.main()
