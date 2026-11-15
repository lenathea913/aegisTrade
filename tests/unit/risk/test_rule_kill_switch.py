"""Rule 7: the kill switch halts every approval, whatever else looks fine."""

from __future__ import annotations

from decimal import Decimal

from aegis_core import MarketContext, OrderIntent, PortfolioState, RiskLimit
from aegis_risk import RejectReason, RiskCheck, evaluate


def test_baseline_is_approved(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    decision = evaluate(small_intent, portfolio, limits, market)

    assert decision.approved is True
    # A new entry is not a risk-reducing order, so that check does not apply.
    expected = tuple(
        check.value for check in RiskCheck if check is not RiskCheck.RISK_REDUCING_ORDER
    )
    assert decision.reasons == expected
    assert decision.max_risk_amount == Decimal("100")


def test_kill_switch_rejects_everything(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    decision = evaluate(small_intent, portfolio, limits, market, kill_switch=True)

    assert decision.approved is False
    assert decision.reasons == (RejectReason.KILL_SWITCH_ACTIVE.value,)
    assert decision.max_risk_amount == 0


def test_kill_switch_does_not_hide_other_failures(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    unlisted = small_intent.model_copy(update={"symbol": "ETH/USDT"})

    decision = evaluate(unlisted, portfolio, limits, market, kill_switch=True)

    assert decision.approved is False
    assert RejectReason.KILL_SWITCH_ACTIVE.value in decision.reasons
    assert any(
        reason.startswith(RejectReason.SYMBOL_NOT_ALLOWED.value) for reason in decision.reasons
    )
    assert decision.max_risk_amount == 0
