"""Alert wiring: fills, exchange failures, risk rejections, kill switch, daily loss."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from aegis_alerting import Alert, Alerter
from aegis_core import MarketContext, Order, OrderIntent, PortfolioState, RiskLimit
from aegis_execution import (
    BrokerFill,
    ExchangeUnavailableError,
    ExecutionGateway,
    TradingPipeline,
)
from aegis_ledger import PortfolioLedger
from aegis_risk import RiskGate


class RecordingAlerter:
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.alerts.append(alert)
        return True

    @property
    def titles(self) -> list[str]:
        return [alert.title for alert in self.alerts]


class FailingBroker:
    name = "binance-testnet"
    live = True

    def submit(
        self, order: Order, *, market_price: Decimal, now: datetime | None = None
    ) -> BrokerFill | None:
        raise ExchangeUnavailableError("timed out")


def limits(**overrides: object) -> RiskLimit:
    values: dict[str, object] = {
        "max_risk_per_trade_pct": Decimal("2"),
        "max_daily_loss_pct": Decimal("5"),
        "max_weekly_loss_pct": Decimal("10"),
        "max_exposure_per_symbol_pct": Decimal("100"),
        "max_total_exposure_pct": Decimal("100"),
        "max_leverage": Decimal("1"),
        "allowed_symbols": frozenset({"BTC/USDT"}),
    }
    values.update(overrides)
    return RiskLimit(**values)


def pipeline(
    gate: RiskGate, broker: Any, alerter: Alerter, ledger: PortfolioLedger | None = None
) -> TradingPipeline:
    return TradingPipeline(
        gate=gate,
        gateway=ExecutionGateway(broker, allow_live_orders=True),
        ledger=ledger or PortfolioLedger(initial_equity=Decimal("10000")),
        alerter=alerter,
    )


def test_a_fill_alerts_with_symbol_side_status_price(
    make_intent: Callable[..., OrderIntent],
    market_at: MarketContext,
    now: datetime,
    gate: RiskGate,
) -> None:
    from aegis_execution import PaperBroker

    alerter = RecordingAlerter()
    result = pipeline(gate, PaperBroker(), alerter).process(
        make_intent(qty=Decimal("0.1"), created_at=now - timedelta(seconds=1)), market_at
    )

    assert result.filled is True
    alert = alerter.alerts[-1]
    assert alert.title == "Order filled"
    assert alert.context["symbol"] == "BTC/USDT"
    assert alert.context["side"] == "BUY"
    assert alert.context["status"] == "FILLED"
    assert alert.context["price"]


def test_an_exchange_failure_alerts_as_a_rejection(
    make_intent: Callable[..., OrderIntent],
    market_at: MarketContext,
    now: datetime,
    gate: RiskGate,
) -> None:
    alerter = RecordingAlerter()
    result = pipeline(gate, FailingBroker(), alerter).process(
        make_intent(qty=Decimal("0.1"), created_at=now - timedelta(seconds=1)), market_at
    )

    assert result.filled is False
    assert alerter.titles == ["Order rejected by exchange"]
    assert alerter.alerts[0].context["status"] == "REJECTED"


def test_kill_switch_rejection_is_critical(
    make_intent: Callable[..., OrderIntent],
    market_at: MarketContext,
    now: datetime,
) -> None:
    from aegis_execution import PaperBroker

    alerter = RecordingAlerter()
    halted = RiskGate(limits=limits(), kill_switch=True)
    result = pipeline(halted, PaperBroker(), alerter).process(
        make_intent(qty=Decimal("0.1"), created_at=now - timedelta(seconds=1)), market_at
    )

    assert result.approved is None
    assert alerter.titles == ["Kill switch active"]
    assert alerter.alerts[0].level.value == "critical"


def test_daily_loss_rejection_is_critical(
    gate: RiskGate,
    make_intent: Callable[..., OrderIntent],
    market: MarketContext,
) -> None:
    alerter = RecordingAlerter()
    portfolio = PortfolioState(equity=Decimal("10000"), daily_realized_pnl=Decimal("-600"))

    decision = gate.evaluate(make_intent(), portfolio, market, alerter=alerter)

    assert decision.approved is False
    assert alerter.titles == ["Daily loss limit reached"]
    assert alerter.alerts[0].level.value == "critical"


def test_gate_stays_silent_without_an_alerter(
    gate: RiskGate, make_intent: Callable[..., OrderIntent], market: MarketContext
) -> None:
    halted = RiskGate(limits=limits(), kill_switch=True)

    decision = halted.evaluate(make_intent(), PortfolioState(equity=Decimal("10000")), market)

    assert decision.approved is False  # nothing raised, nothing sent
