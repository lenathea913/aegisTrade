"""``fetch_open_orders`` / ``verify_credentials`` on the testnet adapter."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from aegis_core import OrderStatus, Side
from aegis_execution import (
    BinanceTestnetBroker,
    ExchangeAuthError,
    from_exchange_symbol,
)

KEY = "testnet-key"
SECRET = "testnet-secret"


def broker(handler: Callable[[httpx.Request], httpx.Response]) -> BinanceTestnetBroker:
    return BinanceTestnetBroker(
        api_key=KEY,
        api_secret=SECRET,
        client=httpx.Client(
            base_url="https://testnet.binance.vision", transport=httpx.MockTransport(handler)
        ),
    )


def open_order(symbol: str = "BTCUSDT", status: str = "NEW") -> dict[str, object]:
    return {
        "orderId": 111,
        "symbol": symbol,
        "side": "BUY",
        "status": status,
        "price": "29000.00",
        "origQty": "0.01",
        "executedQty": "0",
    }


def test_symbol_round_trip() -> None:
    assert from_exchange_symbol("BTCUSDT") == "BTC/USDT"
    assert from_exchange_symbol("WEIRD") == "WEIRD"


def test_open_orders_are_mapped_with_symbol_side_and_price() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/openOrders"
        assert request.url.params["signature"]
        return httpx.Response(200, json=[open_order(), open_order("ETHUSDT", "PARTIALLY_FILLED")])

    orders = broker(handler).fetch_open_orders()

    assert len(orders) == 2
    first = orders[0]
    assert first.exchange_order_id == "111"
    assert first.symbol == "BTC/USDT"
    assert first.side is Side.BUY
    assert first.status is OrderStatus.NEW
    assert first.price is not None and str(first.price) == "29000.00"
    assert first.qty is not None and str(first.qty) == "0.01"
    assert orders[1].status is OrderStatus.PARTIALLY_FILLED


def test_an_empty_list_is_fine() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    assert broker(handler).fetch_open_orders() == ()


def test_a_non_list_payload_is_an_exchange_error() -> None:
    from aegis_execution import ExchangeError

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"msg": "nope"})

    with pytest.raises(ExchangeError):
        broker(handler).fetch_open_orders()


def test_verify_credentials_signs_the_account_request() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"balances": []})

    broker(handler).verify_credentials()

    assert seen["signature"] and seen["timestamp"]


def test_verify_credentials_raises_on_a_bad_key() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": -2015, "msg": "Invalid API-key"})

    with pytest.raises(ExchangeAuthError) as excinfo:
        broker(handler).verify_credentials()

    assert SECRET not in str(excinfo.value) and KEY not in str(excinfo.value)


def test_cancel_returns_the_resulting_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.params["symbol"] == "BTCUSDT"
        return httpx.Response(200, json={"orderId": 5, "status": "CANCELED", "executedQty": "0"})

    state = broker(handler).cancel("5", symbol="BTC/USDT")

    assert state.status is OrderStatus.CANCELED
