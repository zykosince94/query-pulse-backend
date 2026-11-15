"""When to try again: steady intervals on success, exponential backoff on failure, with jitter."""

from __future__ import annotations

import random


def next_delay(interval: float, consecutive_failures: int, max_backoff: float) -> float:
    """Seconds to wait before the next cycle.

    Healthy sources are polled every `interval`. After failures the wait doubles each time
    (interval, 2×, 4×, ...) up to `max_backoff`, so a database that's down isn't hammered,
    and it's still retried, so it recovers on its own when the database returns.
    """
    if consecutive_failures <= 0:
        return interval
    return min(max_backoff, interval * 2.0 ** (consecutive_failures - 1))


def jittered(delay: float, rng: random.Random | None = None, spread: float = 0.1) -> float:
    """Spread waits by ±10%, so many sources don't all poll in the same instant."""
    rng = rng or random.Random()  # noqa: S311 - timing jitter, not security
    return delay * rng.uniform(1 - spread, 1 + spread)
