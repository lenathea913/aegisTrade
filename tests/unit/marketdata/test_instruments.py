"""Instrument precision: parsing ``exchangeInfo`` and the fallback behaviour."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from aegis_marketdata import (
    DEFAULT_PRICE_DECIMALS,
    DEFAULT_QTY_DECIMALS,
    InstrumentRegistry,
    InstrumentSpec,
    fetch_instruments,
    parse_exchange_info,
)

EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.00001000"},
            ],
        },
        {
            "symbol": "SHIBUSDT",
            "status": "TRADING",
            "baseAsset": "SHIB",
            "quoteAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.00000001"},
                {"filterType": "LOT_SIZE", "stepSize": "1.00000000"},
            ],
        },
        {
            "symbol": "OLDUSDT",
            "status": "BREAK",
            "baseAsset": "OLD",
            "quoteAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                {"filterType": "LOT_SIZE", "stepSize": "1"},
            ],
        },
        {
            "symbol": "NOFILTERS",
            "status": "TRADING",
            "baseAsset": "NOPE",
            "quoteAsset": "USDT",
            "filters": [],
        },
    ]
}


def test_exchange_info_becomes_symbol_precision() -> None:
    specs = parse_exchange_info(EXCHANGE_INFO)

    assert set(specs) == {"BTC/USDT", "SHIB/USDT"}
    assert specs["BTC/USDT"].price_decimals == 2
    assert specs["BTC/USDT"].qty_decimals == 5
    assert specs["SHIB/USDT"].price_decimals == 8
    assert specs["SHIB/USDT"].qty_decimals == 0


def test_registry_falls_back_for_unknown_symbols() -> None:
    registry = InstrumentRegistry(parse_exchange_info(EXCHANGE_INFO))

    assert registry.price_decimals("BTC/USDT") == 2
    assert registry.price_decimals("btc-usdt") == 2  # symbols are normalised
    assert registry.qty_decimals("DOGE/USDT") == DEFAULT_QTY_DECIMALS
    assert registry.price_decimals(None) == DEFAULT_PRICE_DECIMALS


def test_registry_can_be_replaced_wholesale() -> None:
    registry = InstrumentRegistry()
    registry.replace({"ETH/USDT": InstrumentSpec("ETH/USDT", Decimal("0.01"), Decimal("0.0001"))})

    assert len(registry) == 1
    assert registry.qty_decimals("ETH/USDT") == 4


def test_fetch_instruments_reads_exchange_info() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json=EXCHANGE_INFO)

    specs = fetch_instruments(
        "https://testnet.binance.vision/",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert requests == ["https://testnet.binance.vision/api/v3/exchangeInfo"]
    assert specs["BTC/USDT"].tick_size == Decimal("0.01")


def test_fetch_instruments_surfaces_http_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(451, json={"msg": "restricted"})

    with pytest.raises(httpx.HTTPStatusError):
        fetch_instruments(
            "https://api.binance.com",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
