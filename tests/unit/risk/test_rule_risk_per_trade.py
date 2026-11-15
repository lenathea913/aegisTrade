"""Rule 2: loss at the stop must stay inside max_risk_per_trade_pct."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest

from aegis_core import MarketContext, OrderIntent, OrderType, PortfolioState, RiskLimit, Side
from aegis_risk import RejectReason, RiskCheck, evaluate


def test_risk_exactly_at_the_cap_is_allowed(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    # 0.2 BTC * 1 000 stop distance = 200 = 2 % of 10 000 -> exactly the cap.
    intent = make_intent(qty=Decimal("0.2"))
    wide_exposure = RiskLimit(
        max_risk_per_trade_pct=Decimal("2"),
        max_exposure_per_symbol_pct=Decimal("100"),
        max_total_exposure_pct=Decimal("100"),
        allowed_symbols=frozenset({"BTC/USDT"}),
    )

    decision = evaluate(intent, portfolio, wide_exposure, market)

    assert decision.approved is True
    assert decision.max_risk_amount == Decimal("200")
    assert RiskCheck.RISK_PER_TRADE_WITHIN_LIMIT.value in decision.reasons


def test_risk_above_the_cap_is_rejected(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    intent = make_intent(qty=Decimal("0.3"))  # 300 > 200

    decision = evaluate(intent, portfolio, limits, market)

    assert decision.approved is False
    assert any(
        reason.startswith(RejectReason.RISK_PER_TRADE_EXCEEDED.value) for reason in decision.reasons
    )
    assert "300.0>200" in " ".join(decision.reasons)
    assert decision.max_risk_amount == 0


def test_wider_stop_increases_measured_risk(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    loose_stop = make_intent(qty=Decimal("0.1"), stop_loss=Decimal("28000"))  # 200 risk
    tighter_stop = make_intent(qty=Decimal("0.1"), stop_loss=Decimal("29500"))  # 50 risk

    assert evaluate(loose_stop, portfolio, limits, market).approved is True
    assert evaluate(loose_stop, portfolio, limits, market).max_risk_amount == Decimal("200")
    assert evaluate(tighter_stop, portfolio, limits, market).max_risk_amount == Decimal("50")


def test_market_intent_uses_the_quote_as_entry(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    intent = make_intent(
        order_type=OrderType.MARKET,
        price=None,
        qty=Decimal("0.1"),
        stop_loss=Decimal("29000"),
        take_profit=None,
    )

    decision = evaluate(intent, portfolio, limits, market)

    assert decision.approved is True
    assert decision.max_risk_amount == Decimal("100")  # (30000 - 29000) * 0.1


def test_market_intent_without_price_or_quote_is_rejected(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    limits: RiskLimit,
) -> None:
    intent = make_intent(
        order_type=OrderType.MARKET, price=None, stop_loss=Decimal("29000"), take_profit=None
    )

    decision = evaluate(intent, portfolio, limits, MarketContext())

    assert decision.approved is False
    assert any(r.startswith(RejectReason.MISSING_ENTRY_PRICE.value) for r in decision.reasons)
    assert RejectReason.MISSING_MARKET_DATA.value + ":BTC/USDT" in decision.reasons
    assert decision.max_risk_amount == 0


def test_stop_already_hit_at_entry_price_is_rejected(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    # A MARKET buy whose stop (30 500) is above the current quote (30 000).
    intent = make_intent(
        order_type=OrderType.MARKET,
        price=None,
        stop_loss=Decimal("30500"),
        take_profit=None,
        qty=Decimal("0.1"),
    )

    decision = evaluate(intent, portfolio, limits, market)

    assert decision.approved is False
    assert any(r.startswith(RejectReason.INVALID_STOP_FOR_SIDE.value) for r in decision.reasons)


def test_zero_stop_distance_is_rejected(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    # Quote == stop: no measurable risk, so no approval (and no zero-risk order).
    intent = make_intent(
        order_type=OrderType.MARKET,
        price=None,
        stop_loss=Decimal("30000"),
        take_profit=None,
        qty=Decimal("0.1"),
    )

    decision = evaluate(intent, portfolio, limits, market)

    assert decision.approved is False
    assert RejectReason.ZERO_STOP_DISTANCE.value in decision.reasons


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_short_side_risk_is_measured_the_same_way(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
    side: Side,
) -> None:
    intent = make_intent(
        side=side,
        stop_loss=Decimal("31000") if side is Side.SELL else Decimal("29000"),
        take_profit=None,
        qty=Decimal("0.1"),
    )

    decision = evaluate(intent, portfolio, limits, market)

    assert decision.approved is True
    assert decision.max_risk_amount == Decimal("100")
