"""The runner trades only fresh, risk-approved signals — and refuses unsafe starts."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from aegis_config import Settings, load_settings
from aegis_core import Candle, MarketQuote, Timeframe
from aegis_db import ActivityWriter, RecordingActivityWriter
from aegis_ledger import PortfolioLedger
from aegis_risk import RiskGate
from aegis_strategy import EmaCrossStrategy
from signal_service import DEFAULT_HISTORY_BARS, SignalService
from trading_runner.runner import RunnerConfigError, TradingRunner, ensure_safe_execution_mode

SYMBOL = "BTC/USDT"
TIMEFRAME = Timeframe.H1
START = datetime(2026, 6, 1, tzinfo=UTC)


def crossover_prefix() -> tuple[Candle, ...]:
    """A downtrend then a recovery: the 50/200 cross needs ~200 bars to exist."""
    price = Decimal("40000")
    moment = START
    candles: list[Candle] = []
    for index in range(398):
        step = Decimal("-0.001") if index < 320 else Decimal("0.0015")
        open_price = price
        close_price = price * (Decimal("1") + step)
        candles.append(
            Candle(
                symbol=SYMBOL,
                timeframe=TIMEFRAME,
                open_time=moment,
                close_time=moment + timedelta(hours=1),
                open=open_price,
                high=max(open_price, close_price),
                low=min(open_price, close_price),
                close=close_price,
                volume=Decimal("10"),
            )
        )
        price = close_price
        moment += timedelta(hours=1)

    # The service only ever sees the trailing window, and the EMA seed depends on
    # where that window starts, so the cross is verified exactly as it is read.
    strategy = EmaCrossStrategy(symbol=SYMBOL, timeframe=TIMEFRAME)
    window = candles[-DEFAULT_HISTORY_BARS:]
    if strategy.on_candle(window[-1], window) is None:
        raise AssertionError("fixture produced no crossover in the trailing window")
    return tuple(candles)


HISTORY = crossover_prefix()
NOW = HISTORY[-1].close_time + timedelta(seconds=5)


class Candles:
    def __init__(self, history: tuple[Candle, ...] = HISTORY) -> None:
        self._history = history

    def recent(self, symbol: str, timeframe: Timeframe, limit: int) -> tuple[Candle, ...]:
        return self._history[-limit:]


class Latest:
    """The newest candle the runner uses for its freshness check."""

    def __init__(self, close_time: datetime) -> None:
        self._close_time = close_time

    def latest(self, symbol: str, timeframe: Timeframe) -> Candle:
        candle = HISTORY[-1].model_copy(update={"close_time": self._close_time})
        return candle


class Quotes:
    """A mutable quote source so a test can move the price into a stop."""

    def __init__(self, price: Decimal) -> None:
        self.price = price

    def latest(self, symbol: str) -> MarketQuote:
        return MarketQuote(symbol=symbol, price=self.price, as_of=NOW)


def settings(**overrides: Any) -> Settings:
    base = load_settings("paper")
    market_data = base.market_data.model_copy(update={"stale_after_seconds": 3600})
    runner = base.runner.model_copy(update={"monitor_enabled": True})
    # Permissive caps: this test is about the runner's own gates, not sizing.
    limits = base.risk.limits.model_copy(
        update={
            "max_exposure_per_symbol_pct": Decimal("100"),
            "max_total_exposure_pct": Decimal("100"),
        }
    )
    risk = base.risk.model_copy(
        update={"kill_switch": overrides.get("kill_switch", False), "limits": limits}
    )
    updates: dict[str, Any] = {"market_data": market_data, "runner": runner, "risk": risk}
    return base.model_copy(update=updates)


def build_runner(
    price: str = "32500",
    activity: ActivityWriter | None = None,
    **kwargs: Any,
) -> tuple[TradingRunner, PortfolioLedger]:
    config = settings(**kwargs)
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    signals = SignalService(
        gate=RiskGate.from_settings(config),
        ledger=ledger,
        candles=Candles(),
        symbols=(SYMBOL,),
        timeframe=TIMEFRAME,
        clock=lambda: NOW,
    )
    runner = TradingRunner(
        settings=config,
        ledger=ledger,
        signals=signals,
        candles=Latest(NOW),
        quotes=Quotes(Decimal(price)),
        writer_factory=lambda: nullcontext(None),
        clock=lambda: NOW,
        activity=activity,
    )
    return runner, ledger


def test_an_open_position_is_never_pyramided() -> None:
    """The same cross must not stack a second entry on top of an open position."""
    runner, ledger = build_runner()

    first = runner.run_entry_cycle()
    assert first.filled == (SYMBOL,)
    qty_after_first = ledger.position(SYMBOL).qty
    assert qty_after_first > 0

    second = runner.run_entry_cycle()

    assert second.converted == ()
    assert second.filled == ()
    assert ledger.position(SYMBOL).qty == qty_after_first


def test_fresh_risk_ok_signal_is_converted_and_filled() -> None:
    runner, ledger = build_runner()

    cycle = runner.run_entry_cycle()

    assert cycle.skipped is None
    assert cycle.generated == 1
    assert len(cycle.converted) == 1
    assert cycle.filled == (SYMBOL,)
    assert ledger.position(SYMBOL).is_flat is False


def test_kill_switch_skips_entries_entirely() -> None:
    runner, ledger = build_runner(kill_switch=True)

    cycle = runner.run_entry_cycle()

    assert cycle.skipped == "kill_switch"
    assert cycle.generated == 0
    assert ledger.position(SYMBOL).is_flat is True


def test_stale_candle_blocks_entry() -> None:
    runner, ledger = build_runner()
    runner._candles = Latest(NOW - timedelta(hours=2))

    cycle = runner.run_entry_cycle()

    assert cycle.skipped == "stale_market_data"
    assert cycle.converted == ()
    assert ledger.position(SYMBOL).is_flat is True


def test_monitor_cycle_closes_a_position_at_its_stop() -> None:
    runner, ledger = build_runner(price="32500")
    assert runner.run_entry_cycle().filled == (SYMBOL,)
    stop = ledger.protection(SYMBOL)
    assert stop is not None

    runner._quotes = Quotes(stop.stop_loss - Decimal("10"))
    actions = runner.run_monitor_cycle()

    assert len(actions) == 1
    assert actions[0].trigger.value == "stop_loss"
    assert actions[0].closed is True
    assert ledger.position(SYMBOL).is_flat is True


def test_activity_feed_records_the_entry_cycle() -> None:
    writer = RecordingActivityWriter()
    runner, _ = build_runner(activity=writer)

    runner.run_entry_cycle()

    assert any(event == "SIGNAL_GENERATED" for event, _, _ in writer.events)
    assert any("Evaluated BTC/USDT" in message for message in writer.messages)


def test_activity_feed_records_a_kill_switch_skip() -> None:
    writer = RecordingActivityWriter()
    runner, _ = build_runner(activity=writer, kill_switch=True)

    runner.run_entry_cycle()

    assert writer.events == [
        (
            "ORDER_REJECTED",
            "WARNING",
            "Kill switch is active: entries are skipped, exits keep running.",
        )
    ]


def test_activity_feed_records_the_monitor_check() -> None:
    writer = RecordingActivityWriter()
    runner, _ = build_runner(activity=writer)

    runner.run_monitor_cycle()

    assert writer.events == [
        (
            "PROTECTION_CHECK",
            "INFO",
            "Checked stop-loss and take-profit levels for 0 protected position(s).",
        )
    ]


def test_live_mode_without_permission_refuses_to_start() -> None:
    base = load_settings("paper")
    execution = base.execution.model_copy(
        update={"mode": base.execution.mode.__class__.LIVE, "broker": "live"}
    )
    unsafe = base.model_copy(update={"execution": execution})

    with pytest.raises(RunnerConfigError):
        ensure_safe_execution_mode(unsafe)
