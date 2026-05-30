from __future__ import annotations

import json


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.expiry = {}
        self.deleted = []

    def ping(self):
        return True

    def lpush(self, key, value):
        self.store.setdefault(key, [])
        self.store[key].insert(0, value)
        return len(self.store[key])

    def rpush(self, key, value):
        self.store.setdefault(key, [])
        self.store[key].append(value)
        return len(self.store[key])

    def rpop(self, key):
        values = self.store.get(key, [])
        if not values:
            return None
        return values.pop()

    def lpop(self, key):
        values = self.store.get(key, [])
        if not values:
            return None
        return values.pop(0)

    def lrange(self, key, start, end):
        values = list(self.store.get(key, []))
        length = len(values)
        if start < 0:
            start = max(length + start, 0)
        if end < 0:
            end = length + end
        return values[start : end + 1]

    def llen(self, key):
        return len(self.store.get(key, []))

    def set(self, key, value, ex=None):
        self.store[key] = value
        if ex is not None:
            self.expiry[key] = ex
        return True

    def get(self, key):
        return self.store.get(key)

    def mget(self, keys):
        return [self.store.get(key) for key in keys]

    def delete(self, *keys):
        count = 0
        for key in keys:
            self.deleted.append(key)
            if key in self.store:
                del self.store[key]
                count += 1
        return count

    def exists(self, key):
        return 1 if key in self.store else 0

    def eval(self, script, numkeys, *args):
        current_task_key, last_outcome_key, marker_key = args[:3]
        observed_task_id = args[3]
        raw = self.store.get(current_task_key)
        if not raw:
            return 0
        task = json.loads(raw)
        if task.get("task_id") != observed_task_id:
            return 0
        self.delete(current_task_key, last_outcome_key)
        self.set(marker_key, "1")
        return 1

    def decoded_list(self, key):
        return [json.loads(item) for item in self.store.get(key, [])]
