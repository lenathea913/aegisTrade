"""The Futures Testnet adapter: isolated margin, leverage, hedge-mode legs.

The venue is never contacted: ``httpx.MockTransport`` answers the calls, so these tests
assert the *sequence and content* of what the adapter would send.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from aegis_core import Order, OrderType, PositionSide, Side
from aegis_execution import (
    BinanceFuturesTestnetBroker,
    NonTestnetEndpointError,
    ensure_futures_testnet_base_url,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def make_order(**overrides: Any) -> Order:
    values: dict[str, Any] = {
        "approval_id": "appr_futures_1",
        "symbol": "BTC/USDT",
        "side": Side.BUY,
        "order_type": OrderType.MARKET,
        "qty": Decimal("0.1"),
        "broker": "binance-futures-testnet",
        "leverage": 3,
    }
    values.update(overrides)
    return Order(**values)


def build(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[BinanceFuturesTestnetBroker, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    broker = BinanceFuturesTestnetBroker(
        api_key="key",
        api_secret="secret",
        client=httpx.Client(transport=httpx.MockTransport(record)),
    )
    return broker, calls


def success(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/marginType"):
        return httpx.Response(200, json={"code": 200, "msg": "success"})
    if path.endswith("/leverage"):
        return httpx.Response(200, json={"symbol": "BTCUSDT", "leverage": 3})
    return httpx.Response(
        200,
        json={
            "orderId": 4242,
            "symbol": "BTCUSDT",
            "status": "FILLED",
            "executedQty": "0.1",
            "cumQuote": "3000",
            "side": "BUY",
            "positionSide": "LONG",
        },
    )


def test_a_mainnet_futures_host_is_refused() -> None:
    with pytest.raises(NonTestnetEndpointError, match="testnet-only"):
        ensure_futures_testnet_base_url("https://fapi.binance.com")

    assert (
        ensure_futures_testnet_base_url("https://testnet.binancefuture.com")
        == "https://testnet.binancefuture.com"
    )


def test_cross_margin_is_not_constructible() -> None:
    with pytest.raises(ValueError, match="ISOLATED"):
        BinanceFuturesTestnetBroker(
            api_key="key",
            api_secret="secret",
            margin_type="CROSSED",
        )


def test_margin_mode_then_leverage_are_set_before_the_order() -> None:
    broker, calls = build(success)

    broker.submit(make_order(), market_price=Decimal("30000"), now=NOW)

    assert [request.url.path for request in calls] == [
        "/fapi/v1/marginType",
        "/fapi/v1/leverage",
        "/fapi/v1/order",
    ]
    assert dict(httpx.QueryParams(calls[0].url.query))["marginType"] == "ISOLATED"
    assert dict(httpx.QueryParams(calls[1].url.query))["leverage"] == "3"
    order_params = dict(httpx.QueryParams(calls[2].url.query))
    assert order_params["side"] == "BUY"
    assert order_params["positionSide"] == "LONG"
    assert order_params["quantity"] == "0.1"


def test_preparation_is_not_repeated_for_the_same_symbol_and_leverage() -> None:
    broker, calls = build(success)

    broker.submit(make_order(), market_price=Decimal("30000"), now=NOW)
    broker.submit(make_order(), market_price=Decimal("30000"), now=NOW)

    paths = [request.url.path for request in calls]
    assert paths.count("/fapi/v1/marginType") == 1
    assert paths.count("/fapi/v1/leverage") == 1
    assert paths.count("/fapi/v1/order") == 2


def test_a_new_leverage_is_sent_again() -> None:
    broker, calls = build(success)

    broker.submit(make_order(leverage=3), market_price=Decimal("30000"), now=NOW)
    broker.submit(make_order(leverage=5), market_price=Decimal("30000"), now=NOW)

    leverages = [
        dict(httpx.QueryParams(request.url.query))["leverage"]
        for request in calls
        if request.url.path.endswith("/leverage")
    ]
    assert leverages == ["3", "5"]


def test_closing_a_long_is_a_sell_on_the_long_leg() -> None:
    broker, calls = build(success)
    closing = make_order(side=Side.SELL, position_side=PositionSide.LONG)

    broker.submit(closing, market_price=Decimal("30000"), now=NOW)

    params = dict(httpx.QueryParams(calls[-1].url.query))
    assert params["side"] == "SELL"
    assert params["positionSide"] == "LONG"
    assert "reduceOnly" not in params  # rejected by Binance in hedge mode


def test_an_already_isolated_symbol_is_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/marginType"):
            return httpx.Response(
                400, json={"code": -4046, "msg": "No need to change margin type."}
            )
        return success(request)

    broker, calls = build(handler)

    broker.submit(make_order(), market_price=Decimal("30000"), now=NOW)

    assert calls[-1].url.path.endswith("/order")


def test_the_fill_is_parsed_from_the_order_response() -> None:
    broker, _ = build(success)

    fill = broker.submit(make_order(), market_price=Decimal("30000"), now=NOW)

    assert fill is not None
    assert fill.exchange_order_id == "4242"
    assert fill.qty == Decimal("0.1")
    assert fill.price == Decimal("30000")  # cumQuote / executedQty
    assert fill.filled_at == NOW


def test_positions_are_read_per_leg() -> None:
    payload = [
        {
            "symbol": "BTCUSDT",
            "positionSide": "SHORT",
            "positionAmt": "-0.2",
            "entryPrice": "30000",
            "markPrice": "29500",
            "unRealizedProfit": "100",
            "leverage": "5",
            "liquidationPrice": "35850",
            "isolatedMargin": "1200",
        },
        {"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "0"},
    ]
    broker, _ = build(lambda request: httpx.Response(200, json=payload))

    positions = broker.fetch_positions()

    assert len(positions) == 1
    leg = positions[0]
    assert leg.position_side is PositionSide.SHORT
    assert leg.qty == Decimal("0.2")
    assert leg.leverage == 5
    assert leg.liquidation_price == Decimal("35850")


def test_a_non_json_error_does_not_leak_the_url_query() -> None:
    broker, _ = build(lambda request: httpx.Response(500, text="<html>down</html>"))

    with pytest.raises(Exception, match="HTTP 500"):
        broker.set_leverage("BTC/USDT", 3)


def test_credentials_are_missing_is_a_typed_error() -> None:
    with pytest.raises(Exception, match="credentials are missing"):
        BinanceFuturesTestnetBroker(api_key="", api_secret="secret")


def test_submit_payload_is_json_encodable() -> None:
    """Guard against Decimal leaking into an assertion payload (test hygiene)."""
    broker, calls = build(success)
    broker.submit(make_order(), market_price=Decimal("30000"), now=NOW)

    json.dumps(dict(httpx.QueryParams(calls[-1].url.query)))


def _protection_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/openOrders"):
        return httpx.Response(
            200,
            json=[
                {"orderId": 11, "type": "STOP_MARKET"},
                {"orderId": 12, "type": "TAKE_PROFIT_MARKET"},
                {"orderId": 13, "type": "LIMIT"},
            ],
        )
    if request.method == "DELETE":
        return httpx.Response(200, json={"orderId": 11, "status": "CANCELED", "executedQty": "0"})
    return httpx.Response(
        200,
        json={"orderId": 9100, "status": "NEW", "executedQty": "0", "symbol": "BTCUSDT"},
    )


def test_protection_orders_close_the_leg_at_a_mark_price_trigger() -> None:
    broker, calls = build(_protection_handler)

    placed = broker.submit_protection_order(
        symbol="BTC/USDT",
        side=Side.SELL,
        position_side=PositionSide.LONG,
        stop_loss=Decimal("29000"),
        take_profit=Decimal("32000"),
        qty=Decimal("0.1"),
    )

    assert placed.stop_order_id == "9100"
    assert placed.take_order_id == "9100"
    orders = [call for call in calls if call.url.path.endswith("/v1/order")]
    assert len(orders) == 2
    stop, take = (dict(call.url.params) for call in orders)
    assert stop["type"] == "STOP_MARKET"
    assert take["type"] == "TAKE_PROFIT_MARKET"
    assert stop["stopPrice"] == "29000"
    assert take["stopPrice"] == "32000"
    # closePosition makes the order unable to open exposure; no quantity is sent.
    assert stop["closePosition"] == "true"
    assert "quantity" not in stop
    assert stop["workingType"] == "MARK_PRICE"
    assert stop["side"] == "SELL"
    assert stop["positionSide"] == "LONG"


def test_only_protection_orders_are_cancelled_when_a_leg_closes() -> None:
    broker, calls = build(_protection_handler)

    cancelled = broker.cancel_protection_orders("BTC/USDT")

    assert cancelled == 2
    deletes = [call for call in calls if call.method == "DELETE"]
    assert [dict(call.url.params)["orderId"] for call in deletes] == ["11", "12"]
