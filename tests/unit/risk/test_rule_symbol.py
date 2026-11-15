"""Rule 1: the symbol must be on the allow-list (an empty list denies everything)."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from aegis_core import MarketContext, OrderIntent, PortfolioState, RiskLimit
from aegis_risk import RejectReason, evaluate


def test_listed_symbol_is_approved(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    assert evaluate(small_intent, portfolio, limits, market).approved is True


def test_unlisted_symbol_is_rejected(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    limits: RiskLimit,
    market: MarketContext,
) -> None:
    intent = make_intent(symbol="ETH/USDT", qty=Decimal("0.1"))

    decision = evaluate(intent, portfolio, limits, market)

    assert decision.approved is False
    assert RejectReason.SYMBOL_NOT_ALLOWED.value + ":ETH/USDT" in decision.reasons


def test_empty_allow_list_denies_every_symbol(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    limits = RiskLimit(allowed_symbols=frozenset())

    decision = evaluate(small_intent, portfolio, limits, market)

    assert decision.approved is False
    assert RejectReason.SYMBOL_NOT_ALLOWED.value + ":BTC/USDT" in decision.reasons


def test_allow_list_is_case_and_separator_insensitive(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    limits = RiskLimit(
        max_risk_per_trade_pct=Decimal("2"),
        max_exposure_per_symbol_pct=Decimal("100"),
        max_total_exposure_pct=Decimal("100"),
        allowed_symbols=frozenset({"btc-usdt"}),
    )
    intent = make_intent(symbol=" btc/usdt ", qty=Decimal("0.1"))

    decision = evaluate(intent, portfolio, limits, market)

    assert decision.approved is True
