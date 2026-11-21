"""Golden determinism: one fixture, one config, one hash.

The audit scripts prove *one* trade is honest. This test proves that the whole run is
**unchanged**: a refactor that shifts one fill by a tick, drifts a fee, or reorders an
exit changes the fingerprint, and the build fails instead of quietly publishing
different numbers.

``tests/fixtures/golden/BTCUSDT_1h_240.csv`` is a committed, deterministic series
(240 hourly bars, fixed formula) so the run never depends on a database, a network or
the machine's clock. The config deliberately exercises every cost path at once:
latency, leverage, funding, taker fees, slippage, shorts and ``close_at_end``.

When a change to ``packages/aegis_backtest`` is *intended*, update ``GOLDEN_DIGEST``
in the same commit and say why in the message. A test that can be "fixed" without an
explanation is not a regression test.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from aegis_backtest import (
    BacktestConfig,
    BacktestResult,
    BacktestTrade,
    ExitReason,
    load_candles_csv,
    run,
)
from aegis_core import Timeframe
from aegis_strategy import EmaCrossParams, EmaCrossStrategy

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests" / "fixtures" / "golden" / "BTCUSDT_1h_240.csv"

#: Fingerprint of the 17 trades below. Any engine change that alters a side, a
#: timestamp, a fill price, a quantity or a net PnL breaks this value.
GOLDEN_DIGEST = "c232c6540a1f2bc3992e65c8fc34f2c0a5f122fcf7bc1ee34da0559c90ec8f47"
GOLDEN_TRADES = 17

PARAMS = EmaCrossParams(
    fast_period=5,
    slow_period=13,
    adx_threshold=None,
    qty=Decimal("0.1"),
    stop_loss_pct=Decimal("1.2"),
    take_profit_pct=Decimal("3"),
)

CONFIG = BacktestConfig(
    initial_equity=Decimal("10000"),
    taker_fee_bps=Decimal("5"),
    maker_fee_bps=Decimal("2"),
    slippage_bps=Decimal("5"),
    funding_rate_bps_per_8h=Decimal("1"),
    latency_bars=1,
    leverage=2,
    allow_short=True,
    close_at_end=True,
)


def fingerprint(trades: tuple[BacktestTrade, ...]) -> str:
    """SHA-256 over the fields the golden test locks down, one trade per line."""
    lines = [
        "|".join(
            (
                trade.side.value,
                trade.entry_time.isoformat(),
                str(trade.entry_price),
                trade.exit_time.isoformat(),
                str(trade.exit_price),
                str(trade.qty),
                str(trade.net_pnl),
            )
        )
        for trade in trades
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def golden_run() -> BacktestResult:
    """Replay the fixture with a fresh strategy (the strategy is stateful)."""
    candles = load_candles_csv(FIXTURE, symbol="BTC/USDT", timeframe=Timeframe.H1)
    strategy = EmaCrossStrategy(symbol="BTC/USDT", timeframe=Timeframe.H1, params=PARAMS)
    return run(strategy, candles, CONFIG)


def test_the_golden_run_matches_its_recorded_fingerprint() -> None:
    result = golden_run()

    assert len(result.trades) == GOLDEN_TRADES
    assert fingerprint(result.trades) == GOLDEN_DIGEST


def test_the_fixture_exercises_every_exit_path() -> None:
    """A golden run that only ever exits on a signal would hide stop/TP regressions."""
    reasons = {trade.exit_reason for trade in golden_run().trades}

    assert ExitReason.STOP_LOSS in reasons
    assert ExitReason.TAKE_PROFIT in reasons
    assert ExitReason.SIGNAL in reasons


def test_the_fingerprint_is_stable_across_runs() -> None:
    assert fingerprint(golden_run().trades) == fingerprint(golden_run().trades)


def test_the_fingerprint_notices_a_timing_change() -> None:
    """One extra bar of latency must move the hash: that is the point of the test."""
    candles = load_candles_csv(FIXTURE, symbol="BTC/USDT", timeframe=Timeframe.H1)
    strategy = EmaCrossStrategy(symbol="BTC/USDT", timeframe=Timeframe.H1, params=PARAMS)
    shifted = run(strategy, candles, CONFIG.model_copy(update={"latency_bars": 2}))

    assert fingerprint(shifted.trades) != GOLDEN_DIGEST


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("slippage_bps", Decimal("6")),
        ("taker_fee_bps", Decimal("6")),
        ("funding_rate_bps_per_8h", Decimal("2")),
    ],
)
def test_the_fingerprint_notices_a_cost_change(field: str, value: Decimal) -> None:
    candles = load_candles_csv(FIXTURE, symbol="BTC/USDT", timeframe=Timeframe.H1)
    strategy = EmaCrossStrategy(symbol="BTC/USDT", timeframe=Timeframe.H1, params=PARAMS)
    altered = run(strategy, candles, CONFIG.model_copy(update={field: value}))

    assert fingerprint(altered.trades) != GOLDEN_DIGEST
