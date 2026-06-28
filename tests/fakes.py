from __future__ import annotations

import json
from fnmatch import fnmatch


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

    def sadd(self, key, *values):
        current = self.store.setdefault(key, set())
        before = len(current)
        current.update(values)
        return len(current) - before

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

    def scan_iter(self, match=None, count=None):
        del count
        keys = list(self.store.keys())
        if match is None:
            for key in keys:
                yield key
            return
        for key in keys:
            if fnmatch(key, match):
                yield key

    def eval(self, script, numkeys, *args):
        if numkeys == 1:
            key = args[0]
            observed_value = args[1]
            if self.store.get(key) == observed_value:
                self.delete(key)
                return 1
            return 0

        current_task_key, last_outcome_key, marker_key = args[:3]
        observed_task_id = args[3]
        raw = self.store.get(current_task_key)
        if not raw:
            return 0
        task = json.loads(raw)
        if task.get("task_id") != observed_task_id:
            return 0
        self.delete(current_task_key, last_outcome_key)
        self.set(marker_key, "1", ex=30)
        return 1

    def decoded_list(self, key):
        return [json.loads(item) for item in self.store.get(key, [])]
