"""PortfolioState / MarketContext: the immutable snapshots the risk gate judges."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aegis_core import MarketContext, MarketQuote, PortfolioState, Position

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def test_portfolio_defaults_are_empty_and_flat() -> None:
    state = PortfolioState(equity=Decimal("10000"))

    assert state.positions == ()
    assert state.daily_realized_pnl == 0
    assert state.weekly_realized_pnl == 0
    assert state.open_positions == ()


def test_unknown_symbol_yields_a_flat_position() -> None:
    state = PortfolioState(equity=Decimal("10000"))

    position = state.position("btc-usdt")

    assert position.symbol == "BTC/USDT"
    assert position.is_flat


def test_position_lookup_is_normalised() -> None:
    held = Position(symbol="BTC/USDT", qty=Decimal("1"), avg_entry=Decimal("30000"))
    state = PortfolioState(equity=Decimal("10000"), positions=(held,))

    assert state.position(" btc/usdt ") is held


def test_open_positions_skip_flat_rows() -> None:
    live = Position(symbol="BTC/USDT", qty=Decimal("1"), avg_entry=Decimal("30000"))
    flat = Position.flat("ETH/USDT")
    state = PortfolioState(equity=Decimal("10000"), positions=(live, flat))

    assert state.open_positions == (live,)


def test_duplicate_positions_are_rejected() -> None:
    held = Position(symbol="BTC/USDT", qty=Decimal("1"), avg_entry=Decimal("30000"))

    with pytest.raises(ValidationError, match="duplicate positions"):
        PortfolioState(equity=Decimal("10000"), positions=(held, held))


def test_duplicate_quotes_are_rejected() -> None:
    quote = MarketQuote(symbol="BTC/USDT", price=Decimal("30000"), as_of=NOW)

    with pytest.raises(ValidationError, match="duplicate quotes"):
        MarketContext(quotes=(quote, quote), now=NOW)


def test_quote_defaults_to_now() -> None:
    quote = MarketQuote(symbol="BTC/USDT", price=Decimal("30000"))

    assert quote.as_of.utcoffset() == timedelta(0)


def test_price_and_age_for_missing_symbol() -> None:
    context = MarketContext(now=NOW)

    assert context.price("BTC/USDT") is None
    assert context.age_seconds("BTC/USDT") is None
    assert context.is_fresh("BTC/USDT", 60) is False


def test_age_and_freshness() -> None:
    context = MarketContext(
        quotes=(
            MarketQuote(
                symbol="BTC/USDT", price=Decimal("30000"), as_of=NOW - timedelta(seconds=10)
            ),
        ),
        now=NOW,
    )

    assert context.price("btc-usdt") == Decimal("30000")
    assert context.age_seconds("BTC/USDT") == 10
    assert context.is_fresh("BTC/USDT", 10) is True
    assert context.is_fresh("BTC/USDT", 9) is False


def test_future_quote_is_not_fresh() -> None:
    context = MarketContext(
        quotes=(
            MarketQuote(
                symbol="BTC/USDT", price=Decimal("30000"), as_of=NOW + timedelta(seconds=5)
            ),
        ),
        now=NOW,
    )

    assert context.age_seconds("BTC/USDT") == -5
    assert context.is_fresh("BTC/USDT", 60) is False


def test_equity_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        PortfolioState(equity=Decimal("0"))
