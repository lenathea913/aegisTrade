"""The testnet adapter: URL guard, HMAC signing, status mapping, typed failures.

Nothing here touches the network: ``httpx.MockTransport`` answers every request, so the
suite proves the request shape and the error mapping without a Binance account.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from aegis_core import Order, OrderStatus, OrderType, Side
from aegis_execution import (
    BinanceTestnetBroker,
    ExchangeAuthError,
    ExchangeUnavailableError,
    NonTestnetEndpointError,
    ensure_testnet_base_url,
)

KEY = "testnet-key-should-never-leak"
SECRET = "testnet-secret-should-never-leak"


def order(order_type: OrderType = OrderType.MARKET, price: str | None = None) -> Order:
    return Order(
        approval_id="appr-1",
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=order_type,
        qty=Decimal("0.01"),
        price=None if price is None else Decimal(price),
        broker="binance-testnet",
    )


def broker(handler: Callable[[httpx.Request], httpx.Response]) -> BinanceTestnetBroker:
    return BinanceTestnetBroker(
        api_key=KEY,
        api_secret=SECRET,
        base_url="https://testnet.binance.vision",
        client=httpx.Client(
            base_url="https://testnet.binance.vision", transport=httpx.MockTransport(handler)
        ),
    )


# ------------------------------------------------------------------- URL guard


def test_testnet_hosts_are_accepted() -> None:
    url = "https://testnet.binance.vision"
    assert ensure_testnet_base_url(url) == url


@pytest.mark.parametrize("url", ["https://api.binance.com", "https://api1.binance.com"])
def test_mainnet_hosts_are_refused(url: str) -> None:
    with pytest.raises(NonTestnetEndpointError):
        ensure_testnet_base_url(url)


def test_mainnet_needs_the_explicit_live_confirmation() -> None:
    assert ensure_testnet_base_url("https://api.binance.com", live_confirm=True)


def test_broker_refuses_to_construct_against_mainnet() -> None:
    with pytest.raises(NonTestnetEndpointError):
        BinanceTestnetBroker(api_key=KEY, api_secret=SECRET, base_url="https://api.binance.com")


def test_missing_credentials_are_a_config_error() -> None:
    with pytest.raises(ExchangeAuthError):
        BinanceTestnetBroker(api_key="", api_secret=SECRET)


# ------------------------------------------------------------------- submission


def test_submit_signs_the_request_and_maps_a_full_fill() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-MBX-APIKEY"] == KEY
        params = dict(request.url.params)
        assert params["symbol"] == "BTCUSDT"
        assert params["side"] == "BUY"
        assert params["newClientOrderId"].startswith("ord")
        assert params["signature"]  # hexdigest of the query; presence is enough here
        assert params["timestamp"].isdigit()
        seen.update(params)
        return httpx.Response(
            200,
            json={
                "orderId": 42,
                "symbol": "BTCUSDT",
                "status": "FILLED",
                "executedQty": "0.01",
                "cummulativeQuoteQty": "300.00",
                "fills": [{"price": "30000", "qty": "0.01", "commission": "0.15"}],
            },
        )

    fill = broker(handler).submit(order(), market_price=Decimal("30000"))

    assert seen["recvWindow"] == "5000"
    assert fill is not None
    assert fill.exchange_order_id == "42"
    assert fill.qty == Decimal("0.01")
    assert fill.price == Decimal("30000")
    assert fill.fee == Decimal("0.15")


def test_an_accepted_but_unfilled_order_returns_no_fill() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"orderId": 7, "status": "NEW", "executedQty": "0"})

    assert broker(handler).submit(order(), market_price=Decimal("30000")) is None


def test_limit_orders_carry_price_and_time_in_force() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"orderId": 8, "status": "NEW", "executedQty": "0"})

    broker(handler).submit(order(OrderType.LIMIT, "29000"), market_price=Decimal("30000"))

    assert captured["type"] == "LIMIT"
    assert captured["price"] == "29000"
    assert captured["timeInForce"] == "GTC"


# --------------------------------------------------------------------- failures


def test_401_becomes_an_auth_error_and_hides_the_secret() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": -2015, "msg": "Invalid API-key"})

    with pytest.raises(ExchangeAuthError) as excinfo:
        broker(handler).submit(order(), market_price=Decimal("30000"))

    message = str(excinfo.value)
    assert "401" in message
    assert SECRET not in message and KEY not in message


def test_timeout_becomes_an_unavailable_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(ExchangeUnavailableError):
        broker(handler).submit(order(), market_price=Decimal("30000"))


def test_fetch_maps_exchange_status_and_average_price() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "orderId": 99,
                "status": "PARTIALLY_FILLED",
                "executedQty": "0.02",
                "cummulativeQuoteQty": "600",
            },
        )

    state = broker(handler).fetch("99", symbol="BTC/USDT")

    assert state.status is OrderStatus.PARTIALLY_FILLED
    assert state.filled_qty == Decimal("0.02")
    assert state.avg_price == Decimal("30000")


def test_ping_never_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert broker(handler).ping() is False


def test_scientific_notation_is_never_sent() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"orderId": 1, "status": "NEW", "executedQty": "0"})

    broker(handler).submit(order(), market_price=Decimal("30000"))

    assert "E" not in captured["quantity"].upper()
    assert json.loads(json.dumps(captured))  # params stayed plain strings
