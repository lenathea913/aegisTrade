"""Exponential backoff with jitter.

Deliberately small and injectable: the sleep function and jitter source are
arguments, so retry behaviour is tested without waiting and without randomness.
"""

from __future__ import annotations

import time
from collections.abc import Callable

__all__ = ["RetryPolicy", "call_with_retry"]


class RetryPolicy:
    """Bounded exponential backoff: ``initial * multiplier**(n-1)``, capped."""

    def __init__(
        self,
        *,
        attempts: int = 5,
        initial_delay: float = 1.0,
        multiplier: float = 2.0,
        max_delay: float = 60.0,
        jitter: Callable[[], float] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be >= 1")
        if initial_delay <= 0 or multiplier <= 1 or max_delay <= 0:
            raise ValueError("delays must be positive and multiplier must be > 1")

        self.attempts = attempts
        self.initial_delay = initial_delay
        self.multiplier = multiplier
        self.max_delay = max_delay
        self._jitter = jitter or (lambda: 1.0)
        self._sleep = sleep

    def delay_for(self, attempt: int) -> float:
        """Delay before retry number ``attempt`` (1-based), jittered and capped."""
        raw = self.initial_delay * (self.multiplier ** (attempt - 1))
        return min(self.max_delay, raw * self._jitter())

    def call[T](self, operation: Callable[[], T]) -> T:
        """Run ``operation``, retrying on any exception until attempts run out.

        The last exception is re-raised unchanged so callers see the real cause.
        """
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                return operation()
            except Exception as exc:
                last_error = exc
                if attempt == self.attempts:
                    break
                self._sleep(self.delay_for(attempt))

        assert last_error is not None  # only reachable after a failed attempt
        raise last_error


def call_with_retry[T](operation: Callable[[], T], policy: RetryPolicy | None = None) -> T:
    """Module-level convenience wrapper around ``RetryPolicy.call``."""
    return (policy or RetryPolicy()).call(operation)
