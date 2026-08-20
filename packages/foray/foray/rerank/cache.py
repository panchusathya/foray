"""In-process TTL LRU for cross-encoder logits.

Keyed on (query hash, candidate id, model, max_length) — never offset or
limit, which must not change the order. A per-id cache survives a one-row
pool change; a request-level cache would not.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Hashable


class TtlLru:
    def __init__(self, maxsize: int, ttl_s: float) -> None:
        self.maxsize = max(0, maxsize)
        self.ttl_s = ttl_s
        self._data: OrderedDict[Hashable, tuple[float, float]] = OrderedDict()

    def _purge(self, now: float) -> None:
        expired = [key for key, (expires, _value) in self._data.items() if expires <= now]
        for key in expired:
            self._data.pop(key, None)

    def get(self, key: Hashable) -> float | None:
        if self.maxsize <= 0:
            return None
        now = time.monotonic()
        hit = self._data.get(key)
        if hit is None:
            return None
        expires, value = hit
        if expires <= now:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def put(self, key: Hashable, value: float) -> None:
        if self.maxsize <= 0:
            return
        now = time.monotonic()
        self._purge(now)
        self._data[key] = (now + self.ttl_s, value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)


def query_key(query: str, model: str, max_length: int) -> str:
    payload = f"{model}\n{max_length}\n{query}".encode()
    return hashlib.sha256(payload).hexdigest()
