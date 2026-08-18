"""A tiny async token-bucket rate limiter (min-interval between calls)."""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    def __init__(self, rpm: int) -> None:
        self.min_interval = 60.0 / max(1, rpm)
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                await asyncio.sleep(self.min_interval - delta)
            self._last = time.monotonic()
