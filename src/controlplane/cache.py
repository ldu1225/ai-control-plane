"""TTL cache with per-key locks to avoid stampedes."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict


class TTLCache:
    def __init__(self, max_entries=512):
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._data = OrderedDict()
        self._key_locks = {}

    def get(self, key):
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return False, None
            if hit[0] <= now:
                del self._data[key]
                return False, None
            self._data.move_to_end(key)
            return True, hit[1]

    def set(self, key, value, ttl):
        if not ttl or ttl <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def delete(self, key):
        with self._lock:
            self._data.pop(key, None)

    def clear(self, prefix=None):
        with self._lock:
            if prefix is None:
                self._data.clear()
                return
            for key in [k for k in self._data if isinstance(k, tuple) and k and k[0] == prefix]:
                del self._data[key]

    def clear_matching(self, predicate):
        """Removes every entry whose key satisfies predicate(key)."""
        with self._lock:
            for key in [k for k in self._data if predicate(k)]:
                del self._data[key]

    def _key_lock(self, key):
        with self._lock:
            lock = self._key_locks.get(key)
            if lock is None:
                if len(self._key_locks) > 4096:
                    self._key_locks = {k: v for k, v in self._key_locks.items() if v.locked()}
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def get_or_compute(self, key, ttl, fn, cacheable=None):
        """Returns (value, cached). Only one thread computes a missing key at a time."""
        found, value = self.get(key)
        if found:
            return value, True
        with self._key_lock(key):
            found, value = self.get(key)
            if found:
                return value, True
            value = fn()
            if cacheable is None or cacheable(value):
                self.set(key, value, ttl)
            return value, False
