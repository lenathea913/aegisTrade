"""RiskGate: the kill switch and staleness window travel with the limits."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from aegis_config import load_settings
from aegis_core import MarketContext, OrderIntent, PortfolioState, RiskLimit
from aegis_risk import RejectReason, RiskGate


def test_gate_approves_the_baseline(
    limits: RiskLimit,
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    gate = RiskGate(limits=limits)

    assert gate.evaluate(small_intent, portfolio, market).approved is True
    assert gate.stale_after_seconds == 60


def test_gate_wires_the_kill_switch_from_settings(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    settings = load_settings("paper", overrides={"risk": {"kill_switch": True}})
    gate = RiskGate.from_settings(settings)

    decision = gate.evaluate(small_intent, portfolio, market)

    assert gate.kill_switch is True
    assert decision.approved is False
    assert RejectReason.KILL_SWITCH_ACTIVE.value in decision.reasons
    assert decision.max_risk_amount == 0


def test_gate_takes_limits_and_staleness_from_settings(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    settings = load_settings("paper")
    gate = RiskGate.from_settings(settings)

    assert gate.limits == settings.risk.limits
    assert gate.stale_after_seconds == settings.market_data.stale_after_seconds

    # paper config allows only BTC/USDT, so an ETH intent is refused by the gate.
    eth = small_intent.model_copy(update={"symbol": "ETH/USDT"})
    decision = gate.evaluate(eth, portfolio, market)
    assert decision.approved is False
    assert any(r.startswith(RejectReason.SYMBOL_NOT_ALLOWED.value) for r in decision.reasons)


def test_gate_uses_the_configured_staleness_window(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    settings = load_settings("paper", overrides={"market_data": {"stale_after_seconds": 1}})
    gate = RiskGate.from_settings(settings)

    decision = gate.evaluate(small_intent, portfolio, market)  # quote is 5 s old

    assert gate.stale_after_seconds == 1
    assert decision.approved is False
    assert any(r.startswith(RejectReason.STALE_MARKET_DATA.value) for r in decision.reasons)


def test_gate_is_immutable(limits: RiskLimit) -> None:
    gate = RiskGate(limits=limits)

    with pytest.raises(ValidationError):
        gate.kill_switch = True  # type: ignore[misc]


def test_gate_defaults_are_conservative(limits: RiskLimit) -> None:
    gate = RiskGate(limits=limits)

    assert gate.kill_switch is False
    assert gate.stale_after_seconds == 60


def test_gate_rejects_unknown_fields(limits: RiskLimit) -> None:
    with pytest.raises(ValidationError):
        RiskGate(limits=limits, max_leverage=Decimal("10"))  # type: ignore[call-arg]
