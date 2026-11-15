"""Rule 8: market data must exist, not come from the future, and be fresh."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aegis_core import MarketContext, MarketQuote, OrderIntent, PortfolioState, RiskLimit
from aegis_risk import RejectReason, RiskCheck, evaluate

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _market(age_seconds: int) -> MarketContext:
    return MarketContext(
        quotes=(
            MarketQuote(
                symbol="BTC/USDT",
                price=Decimal("30000"),
                as_of=NOW - timedelta(seconds=age_seconds),
            ),
        ),
        now=NOW,
    )


def test_fresh_quote_is_accepted(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    limits: RiskLimit,
) -> None:
    decision = evaluate(small_intent, portfolio, limits, _market(5), stale_after_seconds=60)

    assert decision.approved is True
    assert RiskCheck.MARKET_DATA_FRESH.value in decision.reasons


def test_quote_exactly_at_the_limit_is_accepted(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    limits: RiskLimit,
) -> None:
    decision = evaluate(small_intent, portfolio, limits, _market(60), stale_after_seconds=60)

    assert decision.approved is True


def test_stale_quote_is_rejected(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    limits: RiskLimit,
) -> None:
    decision = evaluate(small_intent, portfolio, limits, _market(61), stale_after_seconds=60)

    assert decision.approved is False
    assert any(r.startswith(RejectReason.STALE_MARKET_DATA.value) for r in decision.reasons)
    assert "61s>60s" in " ".join(decision.reasons)


def test_quote_from_the_future_is_treated_as_stale(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    limits: RiskLimit,
) -> None:
    decision = evaluate(small_intent, portfolio, limits, _market(-30), stale_after_seconds=60)

    assert decision.approved is False
    assert any(r.startswith(RejectReason.STALE_MARKET_DATA.value) for r in decision.reasons)


def test_missing_quote_is_rejected(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    limits: RiskLimit,
) -> None:
    decision = evaluate(small_intent, portfolio, limits, MarketContext(now=NOW))

    assert decision.approved is False
    assert RejectReason.MISSING_MARKET_DATA.value + ":BTC/USDT" in decision.reasons


def test_quote_for_another_symbol_does_not_count(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    limits: RiskLimit,
) -> None:
    only_eth = MarketContext(
        quotes=(
            MarketQuote(symbol="ETH/USDT", price=Decimal("2000"), as_of=NOW - timedelta(seconds=1)),
        ),
        now=NOW,
    )

    decision = evaluate(small_intent, portfolio, limits, only_eth)

    assert decision.approved is False
    assert RejectReason.MISSING_MARKET_DATA.value + ":BTC/USDT" in decision.reasons


@pytest.mark.parametrize("max_age", [0, -1])
def test_staleness_window_must_be_positive(max_age: int) -> None:
    from aegis_risk import RiskGate

    with pytest.raises(ValidationError, match="stale_after_seconds"):
        RiskGate(limits=RiskLimit(), stale_after_seconds=max_age)
