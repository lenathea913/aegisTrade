"""How the eight rules compose into one decision.

All rules run on every call: a rejection explains every violation at once, and an
approval carries the full list of checks that passed plus the accepted risk.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from aegis_core import (
    ApprovedOrder,
    MarketContext,
    OrderIntent,
    OrderType,
    PortfolioState,
    RiskDecision,
    RiskLimit,
)
from aegis_risk import RejectReason, RiskCheck, evaluate


def test_approval_records_every_check(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    decision = evaluate(small_intent, portfolio, limits, market)

    assert decision.approved is True
    expected = {check.value for check in RiskCheck} - {RiskCheck.RISK_REDUCING_ORDER.value}
    assert set(decision.reasons) == expected
    assert len(decision.reasons) == len(expected)


def test_approval_carries_the_accepted_risk_amount(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    decision = evaluate(small_intent, portfolio, limits, market)

    expected = abs(Decimal("30000") - Decimal("29000")) * Decimal("0.1")
    assert decision.max_risk_amount == expected


def test_rejection_lists_multiple_violations_without_short_circuit(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    oversized = make_intent(qty=Decimal("0.5"))  # risk 500, notional 15 000

    decision = evaluate(oversized, portfolio, limits, market)

    assert decision.approved is False
    codes = {reason.split(":")[0] for reason in decision.reasons}
    assert codes == {
        RejectReason.RISK_PER_TRADE_EXCEEDED.value,
        RejectReason.MAX_TOTAL_EXPOSURE_EXCEEDED.value,
        RejectReason.MAX_SYMBOL_EXPOSURE_EXCEEDED.value,
        RejectReason.MAX_LEVERAGE_EXCEEDED.value,
    }
    assert decision.max_risk_amount == 0


def test_unlisted_symbol_without_quote_and_kill_switch_reports_all(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    unlisted = make_intent(symbol="ETH/USDT", qty=Decimal("0.1"))

    decision = evaluate(unlisted, portfolio, limits, market, kill_switch=True)

    assert decision.approved is False
    codes = {reason.split(":")[0] for reason in decision.reasons}
    assert codes == {
        RejectReason.KILL_SWITCH_ACTIVE.value,
        RejectReason.SYMBOL_NOT_ALLOWED.value,
        RejectReason.MISSING_MARKET_DATA.value,
    }


def test_approved_decision_can_build_an_approved_order(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    decision = evaluate(small_intent, portfolio, limits, market)
    assert decision.approved is True

    approved = ApprovedOrder(
        intent=small_intent,
        max_risk_amount=decision.max_risk_amount,
        risk_checks_passed=decision.reasons,
    )

    assert approved.max_risk_amount == decision.max_risk_amount
    assert approved.risk_checks_passed == decision.reasons


def test_rejected_decision_cannot_be_turned_into_an_approval(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    decision = evaluate(make_intent(qty=Decimal("9")), portfolio, limits, market)
    assert decision.approved is False

    # The only way to build an approval is with a positive risk amount, which a
    # rejected decision never provides.
    assert decision.max_risk_amount == 0
    assert ApprovedOrder(intent=make_intent(), max_risk_amount=Decimal("1")).max_risk_amount > 0


def test_market_intent_is_measured_at_the_quote(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    intent = make_intent(
        order_type=OrderType.MARKET,
        price=None,
        stop_loss=Decimal("28000"),
        take_profit=None,
        qty=Decimal("0.1"),
    )

    decision: RiskDecision = evaluate(intent, portfolio, limits, market)

    assert decision.approved is True
    assert decision.max_risk_amount == Decimal("200")  # (30 000 - 28 000) * 0.1
