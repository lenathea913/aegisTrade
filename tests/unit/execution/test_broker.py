"""Paper broker maths and the live stub's refusal."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from aegis_config import ExecutionSettings
from aegis_core import Order, OrderType, Side
from aegis_execution import (
    LiveBroker,
    LiveTradingDisabledError,
    PaperBroker,
    build_broker,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def order(
    *,
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
    qty: str = "0.5",
    price: str | None = None,
) -> Order:
    return Order(
        approval_id="appr-1",
        symbol="BTC/USDT",
        side=side,
        order_type=order_type,
        qty=Decimal(qty),
        price=None if price is None else Decimal(price),
        broker="paper",
    )


def test_market_buy_pays_slippage() -> None:
    fill = PaperBroker(fee_bps=Decimal("5"), slippage_bps=Decimal("5")).submit(
        order(), market_price=Decimal("30000"), now=NOW
    )

    assert fill is not None
    assert fill.price == Decimal("30015.000000000000")
    assert fill.fee == Decimal("7.503750000000")
    assert fill.qty == Decimal("0.5")
    assert fill.filled_at == NOW
    assert fill.exchange_order_id.startswith("paper-")


def test_market_sell_receives_less() -> None:
    fill = PaperBroker(fee_bps=Decimal("5"), slippage_bps=Decimal("5")).submit(
        order(side=Side.SELL), market_price=Decimal("30000"), now=NOW
    )

    assert fill is not None
    assert fill.price == Decimal("29985.000000000000")
    assert fill.fee == Decimal("7.496250000000")


def test_zero_costs_are_allowed() -> None:
    fill = PaperBroker(fee_bps=Decimal("0"), slippage_bps=Decimal("0")).submit(
        order(), market_price=Decimal("30000"), now=NOW
    )

    assert fill is not None
    assert fill.price == Decimal("30000")
    assert fill.fee == Decimal("0")


def test_resting_limit_buy_fills_only_when_the_market_reaches_it() -> None:
    broker = PaperBroker(fee_bps=Decimal("5"), slippage_bps=Decimal("5"))
    limit = order(order_type=OrderType.LIMIT, price="30000")

    assert broker.submit(limit, market_price=Decimal("30100"), now=NOW) is None

    fill = broker.submit(limit, market_price=Decimal("29990"), now=NOW)
    assert fill is not None
    assert fill.price == Decimal("30000")
    assert fill.fee == Decimal("7.5")


def test_resting_limit_sell_fills_only_above_the_limit() -> None:
    broker = PaperBroker(fee_bps=Decimal("0"), slippage_bps=Decimal("0"))
    limit = order(side=Side.SELL, order_type=OrderType.LIMIT, price="30000")

    assert broker.submit(limit, market_price=Decimal("29900"), now=NOW) is None

    fill = broker.submit(limit, market_price=Decimal("30100"), now=NOW)
    assert fill is not None
    assert fill.price == Decimal("30000")


def test_invalid_configuration_is_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        PaperBroker(fee_bps=Decimal("-1"))
    with pytest.raises(ValueError, match="market_price"):
        PaperBroker().submit(order(), market_price=Decimal("0"), now=NOW)


def test_live_broker_never_trades() -> None:
    with pytest.raises(LiveTradingDisabledError, match="stub"):
        LiveBroker().submit(order(), market_price=Decimal("30000"), now=NOW)


def test_build_broker_follows_configuration() -> None:
    paper = build_broker(ExecutionSettings(fee_bps=Decimal("10"), slippage_bps=Decimal("2")))
    assert isinstance(paper, PaperBroker)
    assert paper.fee_bps == Decimal("10")
    assert paper.slippage_bps == Decimal("2")

    live = build_broker(ExecutionSettings(mode="live", broker="live"))
    assert isinstance(live, LiveBroker)
