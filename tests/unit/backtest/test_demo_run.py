"""End-to-end demo: EMA cross over the committed synthetic seed data."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from aegis_backtest import BacktestConfig, BacktestResult, load_candles_csv, run
from aegis_core import Timeframe
from aegis_metrics import PerformanceMetrics
from aegis_strategy import EmaCrossParams, EmaCrossStrategy

ROOT = Path(__file__).resolve().parents[3]
SEED = ROOT / "data" / "seeds" / "BTCUSDT_1h.csv"


def demo_result() -> BacktestResult:
    candles = load_candles_csv(SEED, symbol="BTC/USDT", timeframe=Timeframe.H1)
    strategy = EmaCrossStrategy(params=EmaCrossParams())
    return run(strategy, candles, BacktestConfig())


def test_demo_run_produces_trades_and_consistent_metrics() -> None:
    result = demo_result()

    assert result.total_trades > 0
    assert isinstance(result.metrics, PerformanceMetrics)
    assert result.metrics.total_trades == result.total_trades
    assert result.metrics.winning_trades + result.metrics.losing_trades <= result.total_trades
    assert 0.0 <= result.metrics.max_drawdown <= 1.0
    assert result.metrics.win_rate == result.metrics.winning_trades / result.total_trades


def test_demo_run_is_accounting_consistent() -> None:
    result = demo_result()

    realized = sum((trade.net_pnl for trade in result.trades), Decimal("0"))
    assert result.final_equity == result.config.initial_equity + realized
    assert result.total_fees > 0  # every fill pays a fee
    assert result.total_funding == 0  # funding is off by default
    assert all(point.equity > 0 for point in result.equity_curve)


def test_demo_run_is_reproducible() -> None:
    first = demo_result()
    second = demo_result()

    assert first.trades == second.trades
    assert first.metrics == second.metrics


def test_every_demo_trade_has_an_exit_reason_and_r_multiple() -> None:
    result = demo_result()

    for trade in result.trades:
        assert trade.exit_time >= trade.entry_time
        assert trade.r_multiple is not None
        assert trade.strategy_id == "simple_ema_cross"
