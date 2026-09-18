import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from locking import LockAcquisitionError, acquire_lock_with_retry  # noqa: E402


def test_succeeds_first_try_without_sleeping():
    sleeps = []
    calls = []

    def try_lock():
        calls.append(1)

    attempt = acquire_lock_with_retry(
        try_lock, max_attempts=10, base_delay_seconds=1, max_delay_seconds=30, sleep=sleeps.append
    )

    assert attempt == 1
    assert len(calls) == 1
    assert sleeps == []


def test_retries_then_succeeds():
    sleeps = []
    remaining_failures = [2]  # fail twice, succeed on the 3rd attempt

    def try_lock():
        if remaining_failures[0] > 0:
            remaining_failures[0] -= 1
            raise RuntimeError("lock timeout")

    attempt = acquire_lock_with_retry(
        try_lock, max_attempts=10, base_delay_seconds=1, max_delay_seconds=30, sleep=sleeps.append
    )

    assert attempt == 3
    assert len(sleeps) == 2
    # Backoff should be bounded by max_delay_seconds and non-negative.
    assert all(0 <= s <= 30 for s in sleeps)


def test_exhausts_all_attempts_and_raises():
    sleeps = []

    def try_lock():
        raise RuntimeError("lock timeout")

    with pytest.raises(LockAcquisitionError) as exc_info:
        acquire_lock_with_retry(
            try_lock, max_attempts=4, base_delay_seconds=0.1, max_delay_seconds=1, sleep=sleeps.append
        )

    assert "4 attempts" in str(exc_info.value)
    # No sleep after the final, 4th attempt.
    assert len(sleeps) == 3


def test_rejects_invalid_max_attempts():
    with pytest.raises(ValueError):
        acquire_lock_with_retry(lambda: None, max_attempts=0, base_delay_seconds=1, max_delay_seconds=1)
