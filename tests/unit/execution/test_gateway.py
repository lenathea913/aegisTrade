"""Gateway guards: approval required, live switch, price required, fills recorded."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from aegis_core import (
    ApprovedOrder,
    MarketContext,
    MarketQuote,
    OrderIntent,
    OrderStatus,
    OrderType,
    UnapprovedOrderError,
)
from aegis_execution import (
    ExecutionError,
    ExecutionGateway,
    LiveBroker,
    LiveTradingDisabledError,
    PaperBroker,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def test_a_raw_intent_never_reaches_a_broker(market_at: MarketContext) -> None:
    gateway = ExecutionGateway(PaperBroker())

    with pytest.raises(UnapprovedOrderError, match="ApprovedOrder"):
        gateway.execute(cast(Any, market_at), market=market_at)


def test_live_routing_needs_the_second_switch(
    make_approved: Callable[..., ApprovedOrder], market_at: MarketContext
) -> None:
    gateway = ExecutionGateway(LiveBroker(), allow_live_orders=False)

    with pytest.raises(LiveTradingDisabledError, match="allow_live_orders"):
        gateway.execute(make_approved(), market=market_at)


def test_live_broker_stays_a_stub_even_when_enabled(
    make_approved: Callable[..., ApprovedOrder], market_at: MarketContext
) -> None:
    gateway = ExecutionGateway(LiveBroker(), allow_live_orders=True)

    with pytest.raises(LiveTradingDisabledError, match="stub"):
        gateway.execute(make_approved(), market=market_at)


def test_execution_needs_a_price(
    make_intent: Callable[..., OrderIntent],
) -> None:
    intent = make_intent(
        order_type=OrderType.MARKET,
        price=None,
        created_at=NOW - timedelta(seconds=1),
    )
    approved = ApprovedOrder(intent=intent, max_risk_amount=Decimal("200"), approved_at=NOW)

    with pytest.raises(ExecutionError, match="no reference price"):
        ExecutionGateway(PaperBroker()).execute(approved, market=MarketContext(now=NOW))


def test_market_order_fills_at_a_slipped_quote(
    make_approved: Callable[..., ApprovedOrder], market_at: MarketContext
) -> None:
    approved = make_approved(order_type=OrderType.MARKET, price=None)

    result = ExecutionGateway(PaperBroker()).execute(approved, market=market_at)

    assert result.filled
    assert result.order.status is OrderStatus.FILLED
    assert result.order.exchange_order_id == f"paper-{result.order.order_id}"
    assert result.trade is not None
    assert result.trade.price == Decimal("30015.000000000000")
    assert result.trade.qty == Decimal("0.5")
    assert result.trade.order_id == result.order.order_id
    assert result.trade.strategy_id == approved.strategy_id


def test_limit_fills_at_the_limit_price(
    make_approved: Callable[..., ApprovedOrder],
) -> None:
    market = MarketContext(
        quotes=(
            MarketQuote(
                symbol="BTC/USDT",
                price=Decimal("29990"),
                as_of=NOW - timedelta(seconds=1),
            ),
        ),
        now=NOW,
    )

    result = ExecutionGateway(PaperBroker()).execute(make_approved(), market=market)

    assert result.trade is not None
    assert result.trade.price == Decimal("30000")


def test_untouched_limit_is_accepted_but_not_filled(
    make_approved: Callable[..., ApprovedOrder],
) -> None:
    market = MarketContext(
        quotes=(
            MarketQuote(
                symbol="BTC/USDT",
                price=Decimal("31000"),
                as_of=NOW - timedelta(seconds=1),
            ),
        ),
        now=NOW,
    )

    result = ExecutionGateway(PaperBroker()).execute(make_approved(), market=market)

    assert result.filled is False
    assert result.trade is None
    assert result.order.status is OrderStatus.NEW
    assert result.order.exchange_order_id is None


def test_broker_name_is_exposed(market_at: MarketContext) -> None:
    assert ExecutionGateway(PaperBroker()).broker_name == "paper"
    assert ExecutionGateway(LiveBroker()).broker_name == "live"
