"""Rule 6: effective leverage on gross exposure must not exceed max_leverage.

Exposure caps are expressed as percentages of equity and may exceed 100 % (that
is what makes a leverage rule meaningful), so these tests use 1000 % caps with a
1x leverage limit to isolate it.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from aegis_core import MarketContext, OrderIntent, PortfolioState, Position, RiskLimit
from aegis_risk import RejectReason, RiskCheck, evaluate


def loose_limits(max_leverage: str) -> RiskLimit:
    return RiskLimit(
        max_risk_per_trade_pct=Decimal("100"),
        max_daily_loss_pct=Decimal("5"),
        max_weekly_loss_pct=Decimal("10"),
        max_exposure_per_symbol_pct=Decimal("1000"),
        max_total_exposure_pct=Decimal("1000"),
        max_leverage=Decimal(max_leverage),
        allowed_symbols=frozenset({"BTC/USDT"}),
    )


def test_leverage_within_cap_is_approved(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    decision = evaluate(small_intent, portfolio, loose_limits("3"), market)

    assert decision.approved is True
    assert RiskCheck.LEVERAGE_WITHIN_LIMIT.value in decision.reasons


def test_leverage_above_cap_is_rejected(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    intent = make_intent(qty=Decimal("0.5"))  # notional 15 000 -> 1.5x

    decision = evaluate(intent, portfolio, loose_limits("1"), market)

    assert decision.approved is False
    assert any(r.startswith(RejectReason.MAX_LEVERAGE_EXCEEDED.value) for r in decision.reasons)


def test_exactly_max_leverage_is_allowed(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    intent = make_intent(qty=Decimal("0.5"), price=Decimal("20000"), stop_loss=Decimal("19000"))

    decision = evaluate(intent, portfolio, loose_limits("1"), market)

    assert decision.approved is True  # notional 10 000 == 1.0x on 10 000 equity


def test_leverage_counts_existing_positions(
    make_intent: Callable[..., OrderIntent],
    market: MarketContext,
) -> None:
    held = PortfolioState(
        equity=Decimal("10000"),
        positions=(Position(symbol="BTC/USDT", qty=Decimal("0.3"), avg_entry=Decimal("30000")),),
    )
    intent = make_intent(qty=Decimal("0.1"))

    decision = evaluate(intent, held, loose_limits("1"), market)

    # 9 000 held + 3 000 new = 12 000 -> 1.2x
    assert decision.approved is False
    assert any(r.startswith(RejectReason.MAX_LEVERAGE_EXCEEDED.value) for r in decision.reasons)


def test_leverage_scales_with_equity(
    small_intent: OrderIntent,
    market: MarketContext,
) -> None:
    small_account = PortfolioState(equity=Decimal("2500"))  # 3 000 notional = 1.2x
    large_account = PortfolioState(equity=Decimal("10000"))  # 3 000 notional = 0.3x

    small_decision = evaluate(small_intent, small_account, loose_limits("1"), market)
    large_decision = evaluate(small_intent, large_account, loose_limits("1"), market)

    assert small_decision.approved is False
    assert large_decision.approved is True


def test_futures_style_exposure_above_equity_is_possible() -> None:
    limits = RiskLimit(
        max_exposure_per_symbol_pct=Decimal("300"),
        max_total_exposure_pct=Decimal("500"),
        max_leverage=Decimal("3"),
    )
    assert limits.max_total_exposure_pct == Decimal("500")
    assert limits.max_leverage == Decimal("3")
