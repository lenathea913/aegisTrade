"""Retry policy: attempt counting, exponential delays, cap, jitter, re-raise."""

from __future__ import annotations

import pytest

from market_collector.retry import RetryPolicy, call_with_retry


class Recorder:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def test_first_attempt_success_does_not_sleep() -> None:
    recorder = Recorder()
    policy = RetryPolicy(attempts=3, sleep=recorder.sleep)

    assert policy.call(lambda: "ok") == "ok"
    assert recorder.sleeps == []


def test_delays_grow_exponentially() -> None:
    recorder = Recorder()
    policy = RetryPolicy(attempts=4, initial_delay=1.0, multiplier=2.0, sleep=recorder.sleep)
    attempts = {"n": 0}

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise RuntimeError("boom")
        return "ok"

    assert policy.call(flaky) == "ok"
    assert recorder.sleeps == [1.0, 2.0, 4.0]


def test_delay_is_capped() -> None:
    policy = RetryPolicy(attempts=5, initial_delay=1.0, multiplier=2.0, max_delay=3.0)

    assert [policy.delay_for(attempt) for attempt in range(1, 5)] == [1.0, 2.0, 3.0, 3.0]


def test_jitter_scales_the_delay() -> None:
    policy = RetryPolicy(initial_delay=2.0, jitter=lambda: 0.5)

    assert policy.delay_for(1) == 1.0


def test_last_error_is_re_raised_after_exhausting_attempts() -> None:
    recorder = Recorder()
    policy = RetryPolicy(attempts=3, sleep=recorder.sleep)

    def always_fails() -> None:
        raise ValueError("still broken")

    with pytest.raises(ValueError, match="still broken"):
        policy.call(always_fails)
    assert recorder.sleeps == [1.0, 2.0]


def test_policy_validates_its_arguments() -> None:
    with pytest.raises(ValueError, match="attempts"):
        RetryPolicy(attempts=0)
    with pytest.raises(ValueError, match="delays"):
        RetryPolicy(initial_delay=0)
    with pytest.raises(ValueError, match="delays"):
        RetryPolicy(multiplier=1.0)
    with pytest.raises(ValueError, match="delays"):
        RetryPolicy(max_delay=0)


def test_convenience_wrapper_retries() -> None:
    calls = {"n": 0}

    def flaky() -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("once")
        return calls["n"]

    policy = RetryPolicy(attempts=2, initial_delay=0.01, sleep=lambda _: None)

    assert call_with_retry(flaky, policy) == 2
