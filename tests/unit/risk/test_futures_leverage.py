"""The risk gate's futures rules: leverage cap, margin fit, liquidation distance."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from aegis_core import MarketContext, OrderIntent, PortfolioState, Position, RiskLimit, Side
from aegis_risk import RejectReason, RiskCheck, evaluate


def futures_limits(max_leverage: str = "5") -> RiskLimit:
    return RiskLimit(
        max_risk_per_trade_pct=Decimal("2"),
        max_daily_loss_pct=Decimal("5"),
        max_weekly_loss_pct=Decimal("10"),
        max_exposure_per_symbol_pct=Decimal("500"),
        max_total_exposure_pct=Decimal("1000"),
        max_leverage=Decimal(max_leverage),
        allowed_symbols=frozenset({"BTC/USDT"}),
    )


def test_requested_leverage_above_the_cap_is_rejected(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    intent = make_intent(qty=Decimal("0.2"), price=Decimal("30000"), stop_loss=Decimal("29400"))
    intent = intent.model_copy(update={"leverage": 3})

    decision = evaluate(intent, portfolio, futures_limits("2"), market)

    assert decision.approved is False
    assert any(
        reason.startswith(RejectReason.LEVERAGE_CAP_EXCEEDED.value) for reason in decision.reasons
    )


def test_a_consistent_leveraged_entry_is_approved(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    intent = make_intent(qty=Decimal("0.2"), price=Decimal("30000"), stop_loss=Decimal("29400"))
    intent = intent.model_copy(update={"leverage": 2})

    decision = evaluate(intent, portfolio, futures_limits("5"), market)

    assert decision.approved is True
    assert RiskCheck.LEVERAGE_WITHIN_CAP.value in decision.reasons
    assert RiskCheck.MARGIN_SUFFICIENT.value in decision.reasons
    assert RiskCheck.LIQUIDATION_BEYOND_STOP.value in decision.reasons


def test_notional_that_does_not_fit_the_declared_margin_is_rejected(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    # 1 BTC at 30 000 = 30 000 notional; at 1x on 10 000 equity that needs 20 000 more.
    intent = make_intent(qty=Decimal("1"), price=Decimal("30000"), stop_loss=Decimal("29500"))

    decision = evaluate(intent, portfolio, futures_limits("5"), market)

    assert decision.approved is False
    assert any(
        reason.startswith(RejectReason.INSUFFICIENT_LEVERAGE.value) for reason in decision.reasons
    )


def test_a_stop_beyond_the_liquidation_price_is_rejected(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    """At 5x a long liquidates 19.5 % down, so a 25 % stop can never be reached."""
    intent = make_intent(qty=Decimal("0.2"), price=Decimal("30000"), stop_loss=Decimal("22500"))
    intent = intent.model_copy(update={"leverage": 5})

    decision = evaluate(intent, portfolio, futures_limits("5"), market)

    assert decision.approved is False
    assert any(
        reason.startswith(RejectReason.LIQUIDATION_TOO_CLOSE.value) for reason in decision.reasons
    )


def test_a_short_is_judged_symmetrically(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    """A 5x short liquidates ~19.5 % above entry; the stop must sit inside that."""
    safe = make_intent(
        side=Side.SELL,
        qty=Decimal("0.1"),
        price=Decimal("30000"),
        stop_loss=Decimal("31200"),
        take_profit=Decimal("28800"),
    )
    too_wide = safe.model_copy(update={"stop_loss": Decimal("36000"), "leverage": 5})
    safe = safe.model_copy(update={"leverage": 5})

    assert evaluate(safe, portfolio, futures_limits("5"), market).approved is True
    rejected = evaluate(too_wide, portfolio, futures_limits("5"), market)
    assert rejected.approved is False
    assert any(
        reason.startswith(RejectReason.LIQUIDATION_TOO_CLOSE.value) for reason in rejected.reasons
    )


def test_reducing_orders_skip_the_leverage_checks(
    make_intent: Callable[..., OrderIntent],
    market: MarketContext,
) -> None:
    """Closing a position cannot need more margin; the caps must not trap it."""
    held = PortfolioState(
        equity=Decimal("10000"),
        positions=(
            Position(symbol="BTC/USDT", qty=Decimal("0.2"), avg_entry=Decimal("30000"), leverage=5),
        ),
    )
    intent = make_intent(
        side=Side.SELL,
        qty=Decimal("0.2"),
        price=Decimal("30000"),
        stop_loss=Decimal("30100"),
        take_profit=None,
    )

    decision = evaluate(intent, held, futures_limits("1"), market)

    assert decision.approved is True
    assert RiskCheck.RISK_REDUCING_ORDER.value in decision.reasons
