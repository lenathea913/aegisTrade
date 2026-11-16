"""Signal service: generation, risk preview, conversion and expiry.

The property that matters: conversion asks the risk engine *again* and can refuse.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aegis_core import Candle, RiskLimit, Side, SignalStatus, Timeframe, Trade
from aegis_ledger import PortfolioLedger
from aegis_risk import RiskGate
from aegis_strategy import EmaCrossStrategy
from signal_service import (
    SignalNotFoundError,
    SignalPositionOpenError,
    SignalRiskRejectedError,
    SignalService,
    SignalStateError,
)

SYMBOL = "BTC/USDT"
TIMEFRAME = Timeframe.H1


def series() -> tuple[Candle, ...]:
    """Down-then-up prices with enough bars for the demo EMA cross."""
    price = Decimal("30000")
    moment = datetime(2026, 6, 1, tzinfo=UTC)
    candles: list[Candle] = []
    for index in range(120):
        step = Decimal("-0.004") if index < 80 else Decimal("0.006")
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
    return tuple(candles)


def crossover_history() -> tuple[Candle, ...]:
    """Candles up to and including the first crossover the strategy reports."""
    candles = series()
    strategy = EmaCrossStrategy(symbol=SYMBOL, timeframe=TIMEFRAME)
    for index in range(strategy.warmup_bars, len(candles)):
        if strategy.on_candle(candles[index], candles[: index + 1]) is not None:
            return candles[: index + 1]
    raise AssertionError("fixture produced no crossover")


class Repo:
    def __init__(self, candles: tuple[Candle, ...]) -> None:
        self._candles = candles

    def recent(self, symbol: str, timeframe: Timeframe, limit: int) -> tuple[Candle, ...]:
        return self._candles[-limit:]


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def gate(**overrides: object) -> RiskGate:
    values: dict[str, object] = {
        "max_risk_per_trade_pct": Decimal("2"),
        "max_daily_loss_pct": Decimal("5"),
        "max_weekly_loss_pct": Decimal("10"),
        "max_exposure_per_symbol_pct": Decimal("100"),
        "max_total_exposure_pct": Decimal("100"),
        "max_leverage": Decimal("1"),
        "allowed_symbols": frozenset({SYMBOL}),
    }
    values.update(overrides)
    return RiskGate(limits=RiskLimit(**values))


def build_service(
    *,
    risk_gate: RiskGate | None = None,
    ttl_seconds: int = 3600,
    clock: Clock | None = None,
    ledger: PortfolioLedger | None = None,
) -> tuple[SignalService, PortfolioLedger, Clock]:
    candles = crossover_history()
    moment = clock or Clock(candles[-1].close_time + timedelta(seconds=5))
    book = ledger or PortfolioLedger(initial_equity=Decimal("10000"))
    service = SignalService(
        gate=risk_gate or gate(),
        ledger=book,
        candles=Repo(candles),
        symbols=(SYMBOL,),
        timeframe=TIMEFRAME,
        clock=moment,
        ttl_seconds=ttl_seconds,
    )
    return service, book, moment


def test_generation_produces_a_risk_checked_signal() -> None:
    service, _, _ = build_service()

    signals = service.generate()

    assert len(signals) == 1
    signal = signals[0]
    assert signal.status is SignalStatus.RISK_OK
    assert signal.direction.value == "LONG"
    assert signal.risk_reward > 0
    assert signal.expires_at == signal.created_at + timedelta(hours=1)
    assert signal.market_context["bars"] == len(crossover_history())
    assert service.get(signal.signal_id).signal_id == signal.signal_id


def test_generation_marks_a_risky_signal_blocked() -> None:
    service, _, _ = build_service(
        risk_gate=gate(
            max_exposure_per_symbol_pct=Decimal("5"), max_total_exposure_pct=Decimal("6")
        )
    )

    signals = service.generate()

    assert len(signals) == 1
    assert signals[0].status is SignalStatus.BLOCKED_BY_RISK
    assert any(
        reason.startswith("max_exposure_per_symbol_exceeded")
        for reason in signals[0].status_reasons
    )


def test_no_signal_without_candles() -> None:
    service = SignalService(
        gate=gate(),
        ledger=PortfolioLedger(initial_equity=Decimal("10000")),
        candles=Repo(()),
        symbols=(SYMBOL,),
        timeframe=TIMEFRAME,
    )

    assert service.generate() == ()


def test_conversion_returns_a_draft_intent_and_does_not_execute() -> None:
    service, ledger, _ = build_service()
    signal = service.generate()[0]
    equity_before = ledger.equity

    conversion = service.convert(signal.signal_id)

    assert conversion.signal.status is SignalStatus.CONVERTED
    assert conversion.intent.side is Side.BUY
    assert conversion.intent.take_profit == signal.take_profit
    assert conversion.decision.approved is True
    # Nothing was executed: the book is untouched.
    assert ledger.equity == equity_before
    assert ledger.position(SYMBOL).is_flat
    assert service.get(signal.signal_id).status is SignalStatus.CONVERTED


def test_conversion_reruns_the_gate_and_can_be_refused() -> None:
    """A passing preview is not an authorisation: limits or PnL may have moved."""
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    service, _, _ = build_service(ledger=ledger)
    signal = service.generate()[0]

    # Realize a large loss after the preview: the daily floor is now exhausted.
    for side, price, offset in (
        (Side.BUY, Decimal("1000"), -2),
        (Side.SELL, Decimal("1"), -1),
    ):
        ledger.apply_trade(
            Trade(
                order_id=f"ord_loss_{offset}",
                symbol=SYMBOL,
                side=side,
                qty=Decimal("1"),
                price=price,
                fee=Decimal("0"),
                filled_at=signal.created_at + timedelta(seconds=offset),
                strategy_id="test",
            )
        )

    with pytest.raises(SignalRiskRejectedError) as excinfo:
        service.convert(signal.signal_id)

    assert any(reason.startswith("daily_loss_limit_reached") for reason in excinfo.value.reasons)
    assert service.get(signal.signal_id).status is SignalStatus.BLOCKED_BY_RISK


def open_position(ledger: PortfolioLedger, *, at: datetime) -> None:
    ledger.apply_trade(
        Trade(
            order_id="ord_open",
            symbol=SYMBOL,
            side=Side.BUY,
            qty=Decimal("0.1"),
            price=Decimal("30000"),
            fee=Decimal("0"),
            filled_at=at,
            strategy_id="simple_ema_cross",
        )
    )


def test_conversion_is_refused_while_a_position_is_open() -> None:
    """The API/UI half of the anti-pyramiding rule the runner already enforces."""
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    service, _, _ = build_service(ledger=ledger)
    signal = service.generate()[0]
    assert signal.status is SignalStatus.RISK_OK
    open_position(ledger, at=signal.created_at)

    with pytest.raises(SignalPositionOpenError, match="position already open"):
        service.convert(signal.signal_id)

    # Refused, not consumed: the idea is still RISK_OK for a retry after the exit.
    assert service.get(signal.signal_id).status is SignalStatus.RISK_OK


def test_conversion_works_again_once_the_position_is_flat() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    service, _, _ = build_service(ledger=ledger)
    signal = service.generate()[0]
    open_position(ledger, at=signal.created_at)
    ledger.apply_trade(
        Trade(
            order_id="ord_close",
            symbol=SYMBOL,
            side=Side.SELL,
            qty=Decimal("0.1"),
            price=Decimal("30000"),
            fee=Decimal("0"),
            filled_at=signal.created_at + timedelta(seconds=1),
            strategy_id="position_monitor",
        )
    )

    conversion = service.convert(signal.signal_id)

    assert conversion.signal.status is SignalStatus.CONVERTED
    assert ledger.position(SYMBOL).is_flat


def test_converting_twice_is_refused() -> None:
    service, _, _ = build_service()
    signal = service.generate()[0]
    service.convert(signal.signal_id)

    with pytest.raises(SignalStateError):
        service.convert(signal.signal_id)


def test_blocked_signal_cannot_be_converted() -> None:
    service, _, _ = build_service(
        risk_gate=gate(
            max_exposure_per_symbol_pct=Decimal("5"), max_total_exposure_pct=Decimal("6")
        )
    )
    signal = service.generate()[0]

    # Conversion re-runs the gate; a blocked signal is refused there, not by state.
    with pytest.raises(SignalRiskRejectedError):
        service.convert(signal.signal_id)


def test_ignore_is_final() -> None:
    service, _, _ = build_service()
    signal = service.generate()[0]

    ignored = service.ignore(signal.signal_id)

    assert ignored.status is SignalStatus.IGNORED
    with pytest.raises(SignalStateError):
        service.convert(signal.signal_id)


def test_signals_expire_and_cannot_be_converted() -> None:
    clock = Clock(crossover_history()[-1].close_time + timedelta(seconds=5))
    service, _, _ = build_service(ttl_seconds=1, clock=clock)
    signal = service.generate()[0]

    clock.now = clock.now + timedelta(seconds=30)

    assert service.list_signals()[0].status is SignalStatus.EXPIRED
    with pytest.raises(SignalStateError):
        service.convert(signal.signal_id)


def test_unknown_signal_is_a_lookup_error() -> None:
    service, _, _ = build_service()

    with pytest.raises(SignalNotFoundError):
        service.get("sig_missing")
