"""Ranking must pick the strongest signal, not the first ticker in the alphabet.

Every signal the demo strategy produces has ``risk_reward == 2`` (fixed 2 % stop,
4 % target), so ordering by reward:risk was ordering by symbol name. These tests pin
the replacement: ADX and distance beyond the slow EMA decide, and the ticker is only a
tie-break.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aegis_core import Signal, SignalDirection, SignalStatus, Timeframe
from trading_runner.ranking import rank_candidates, signal_strength, strength_from_context

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def make_signal(
    symbol: str,
    *,
    adx: object = None,
    distance_pct: object = None,
    status: SignalStatus = SignalStatus.RISK_OK,
) -> Signal:
    """A LONG signal carrying the strength metrics the service now records."""
    context: dict[str, object] = {"bars": 300}
    if adx is not None:
        context["adx"] = adx
    if distance_pct is not None:
        context["ema_distance_pct"] = distance_pct
    return Signal(
        symbol=symbol,
        timeframe=Timeframe.H1,
        strategy_id="simple_ema_cross",
        strategy_version="1",
        direction=SignalDirection.LONG,
        entry_ref=Decimal("100"),
        stop_loss=Decimal("98"),
        take_profit=Decimal("104"),
        risk_reward=Decimal("2"),
        market_context=context,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        status=status,
    )


def test_the_strongest_trend_wins_against_the_alphabet() -> None:
    """AAA is alphabetically first; ZZZ is the better trade and must be offered first."""
    weak = make_signal("AAA/USDT", adx=26, distance_pct=0.02)
    strong = make_signal("ZZZ/USDT", adx=48, distance_pct=1.4)

    ranked = rank_candidates([weak, strong])

    assert [signal.symbol for signal in ranked] == ["ZZZ/USDT", "AAA/USDT"]
    assert signal_strength(strong) > signal_strength(weak)


def test_distance_beyond_the_ema_breaks_equal_trend_strength() -> None:
    """Same ADX, different momentum: the cross that travelled further scores higher."""
    stalled = make_signal("AAA/USDT", adx=30, distance_pct=0.1)
    extended = make_signal("ZZZ/USDT", adx=30, distance_pct=1.5)

    assert [s.symbol for s in rank_candidates([stalled, extended])] == [
        "ZZZ/USDT",
        "AAA/USDT",
    ]


def test_reward_to_risk_is_ignored_because_it_is_always_the_same() -> None:
    """The old key cannot separate these two signals; the new one can."""
    weak = make_signal("AAA/USDT", adx=25.5, distance_pct=0.0)
    strong = make_signal("BBB/USDT", adx=41.0, distance_pct=0.3)

    assert weak.risk_reward == strong.risk_reward
    assert [s.symbol for s in rank_candidates([weak, strong])] == ["BBB/USDT", "AAA/USDT"]


def test_signals_without_metrics_do_not_crash_and_sort_last() -> None:
    """A signal stored before the metric existed still ranks, just at the bottom."""
    legacy = make_signal("AAA/USDT")
    measured = make_signal("ZZZ/USDT", adx=27, distance_pct=0.1)

    assert signal_strength(legacy) == 0
    assert [s.symbol for s in rank_candidates([legacy, measured])] == [
        "ZZZ/USDT",
        "AAA/USDT",
    ]


def test_equal_strength_falls_back_to_a_stable_order() -> None:
    """Ties are deterministic, so a scan is reproducible; the ticker is not the rule."""
    first = make_signal("ZZZ/USDT", adx=31, distance_pct=0.5)
    second = make_signal("AAA/USDT", adx=31, distance_pct=0.5)

    assert [s.symbol for s in rank_candidates([first, second])] == ["AAA/USDT", "ZZZ/USDT"]


def test_malformed_metrics_are_treated_as_absent() -> None:
    """A non-numeric value from an older payload must not raise."""
    broken = {"adx": "n/a", "ema_distance_pct": None}

    assert strength_from_context(broken) == Decimal("0")
    assert strength_from_context({}) == Decimal("0")


def test_strength_is_the_documented_weighted_sum() -> None:
    """One percent beyond the EMA is worth ten ADX points."""
    context = {"adx": 30, "ema_distance_pct": 1.0}

    assert strength_from_context(context) == Decimal("40")
