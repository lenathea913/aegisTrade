"""Rule 3: daily and weekly realized-loss windows.

Reaching the floor rejects (the floor is the stop signal); being just under it
still allows a trade.
"""

from __future__ import annotations

from decimal import Decimal

from aegis_core import MarketContext, OrderIntent, PortfolioState, RiskLimit
from aegis_risk import RejectReason, RiskCheck, evaluate


def test_losses_below_the_daily_floor_are_allowed(
    small_intent: OrderIntent,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    state = PortfolioState(equity=Decimal("10000"), daily_realized_pnl=Decimal("-499.99"))

    decision = evaluate(small_intent, state, limits, market)

    assert decision.approved is True
    assert RiskCheck.DAILY_LOSS_WITHIN_LIMIT.value in decision.reasons


def test_reaching_the_daily_floor_rejects(
    small_intent: OrderIntent,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    state = PortfolioState(equity=Decimal("10000"), daily_realized_pnl=Decimal("-500"))

    decision = evaluate(small_intent, state, limits, market)

    assert decision.approved is False
    assert any(
        reason.startswith(RejectReason.DAILY_LOSS_LIMIT_REACHED.value)
        for reason in decision.reasons
    )


def test_exceeding_the_daily_floor_rejects(
    small_intent: OrderIntent,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    state = PortfolioState(equity=Decimal("10000"), daily_realized_pnl=Decimal("-750"))

    decision = evaluate(small_intent, state, limits, market)

    assert decision.approved is False
    assert any(
        reason.startswith(RejectReason.DAILY_LOSS_LIMIT_REACHED.value)
        for reason in decision.reasons
    )
    assert decision.max_risk_amount == 0


def test_profit_does_not_block_trading(
    small_intent: OrderIntent,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    state = PortfolioState(equity=Decimal("10000"), daily_realized_pnl=Decimal("300"))

    assert evaluate(small_intent, state, limits, market).approved is True


def test_reaching_the_weekly_floor_rejects(
    small_intent: OrderIntent,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    state = PortfolioState(
        equity=Decimal("10000"),
        daily_realized_pnl=Decimal("0"),
        weekly_realized_pnl=Decimal("-1000"),  # 10 % weekly limit reached
    )

    decision = evaluate(small_intent, state, limits, market)

    assert decision.approved is False
    assert any(
        reason.startswith(RejectReason.WEEKLY_LOSS_LIMIT_REACHED.value)
        for reason in decision.reasons
    )


def test_both_floors_can_fail_together(
    small_intent: OrderIntent,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    state = PortfolioState(
        equity=Decimal("10000"),
        daily_realized_pnl=Decimal("-600"),
        weekly_realized_pnl=Decimal("-1200"),
    )

    decision = evaluate(small_intent, state, limits, market)

    assert decision.approved is False
    assert any(r.startswith(RejectReason.DAILY_LOSS_LIMIT_REACHED.value) for r in decision.reasons)
    assert any(r.startswith(RejectReason.WEEKLY_LOSS_LIMIT_REACHED.value) for r in decision.reasons)


def test_limits_scale_with_equity(
    small_intent: OrderIntent,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    # -500 is exactly the 5 % floor on 10 000 equity, but only 2.5 % on 20 000.
    same_loss_smaller_account = PortfolioState(
        equity=Decimal("10000"), daily_realized_pnl=Decimal("-500")
    )
    same_loss_larger_account = PortfolioState(
        equity=Decimal("20000"), daily_realized_pnl=Decimal("-500")
    )

    assert evaluate(small_intent, same_loss_smaller_account, limits, market).approved is False
    assert evaluate(small_intent, same_loss_larger_account, limits, market).approved is True
