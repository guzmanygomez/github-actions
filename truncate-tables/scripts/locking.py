"""Generic retry-with-backoff helper for acquiring a table lock.

Engine-agnostic: it just calls a zero-arg callable that either succeeds or
raises, and applies full-jitter exponential backoff between attempts. The
engine-specific "try to lock once, with a short timeout" logic lives in
db_adapters.py.
"""
from __future__ import annotations

import random
import time
from typing import Callable, Optional


class LockAcquisitionError(Exception):
    pass


def acquire_lock_with_retry(
    try_lock: Callable[[], None],
    max_attempts: int,
    base_delay_seconds: float,
    max_delay_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Optional[Callable[[int, int, float, Exception], None]] = None,
) -> int:
    """Calls try_lock() until it succeeds or max_attempts is exhausted.

    Returns the attempt number (1-indexed) that succeeded. Raises
    LockAcquisitionError, chained from the last underlying error, once every
    attempt has failed.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            try_lock()
            return attempt
        except Exception as exc:  # noqa: BLE001 - engine-specific lock/timeout errors
            last_exc = exc
            if attempt == max_attempts:
                break
            # Full jitter: uniform(0, min(cap, base * 2**(attempt-1))).
            delay = random.uniform(0, min(max_delay_seconds, base_delay_seconds * (2 ** (attempt - 1))))
            if on_retry:
                on_retry(attempt, max_attempts, delay, exc)
            sleep(delay)

    raise LockAcquisitionError(
        f"failed to acquire table lock after {max_attempts} attempts: {last_exc}"
    ) from last_exc
