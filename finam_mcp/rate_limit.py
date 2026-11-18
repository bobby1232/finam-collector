from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from mcp.server.auth.middleware.auth_context import get_access_token


class RateLimitExceeded(RuntimeError):
    pass


class SlidingWindowRateLimiter:
    def __init__(self, requests: int, window_seconds: int) -> None:
        self.requests = requests
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self) -> None:
        token = get_access_token()
        key = (token.subject or token.client_id) if token else "unauthenticated"
        now = time.monotonic()
        cutoff = now - self.window_seconds
        async with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.requests:
                raise RateLimitExceeded(
                    f"Rate limit exceeded: {self.requests} requests per "
                    f"{self.window_seconds} seconds"
                )
            events.append(now)
