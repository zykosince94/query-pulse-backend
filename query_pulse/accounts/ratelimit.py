"""A login rate limit: too many failed sign-ins from one client locks it out for a while.

In memory, which is correct for the one API instance the blueprint allows (a restart
clears it). Only failures count, so a user who signs in correctly is never slowed down.
While a client is locked out, the password isn't even checked, so the limit can't be used
to keep guessing.

Accepted limitation (documented): behind a reverse proxy every client has the proxy's
address unless the proxy's X-Forwarded-For is trusted (uvicorn --proxy-headers with
--forwarded-allow-ips), and then the limit is shared by everyone behind it.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable


class LoginRateLimiter:
    def __init__(
        self,
        max_failures: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.clock = clock
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, client: str) -> deque[float]:
        failures = self._failures.get(client)
        if failures is None:
            return deque()
        cutoff = self.clock() - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            del self._failures[client]
        return failures

    def retry_after(self, client: str) -> int | None:
        """Seconds until this client may try again, or None if it isn't locked out."""
        failures = self._recent(client)
        if len(failures) < self.max_failures:
            return None
        # Locked until the oldest failure in the window ages out.
        return max(1, math.ceil(failures[0] + self.window_seconds - self.clock()))

    def record_failure(self, client: str) -> None:
        self._recent(client)
        self._failures.setdefault(client, deque()).append(self.clock())
        # Bound memory against someone cycling through addresses.
        if len(self._failures) > 10_000:
            oldest = min(self._failures, key=lambda c: self._failures[c][-1])
            del self._failures[oldest]
