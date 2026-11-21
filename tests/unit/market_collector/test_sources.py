"""Candle sources: CSV seeds and Binance klines (mocked transport, no network)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from aegis_core import Timeframe
from market_collector.sources import (
    FUTURES_KLINES_PATH,
    BinanceFuturesSource,
    BinanceKlineSource,
    CollectorError,
    CsvCandleSource,
    build_source,
)

CSV_HEADER = "open_time,open,high,low,close,volume\n"
CSV_ROWS = "".join(f"2024-01-01T0{hour}:00:00Z,100,101,99,100.5,10\n" for hour in range(3))

KLINE = [
    1704067200000,
    "30000.00",
    "30100.00",
    "29900.00",
    "30050.00",
    "12.5",
    1704070799999,
    "375000.0",
    42,
]


# ---------------------------------------------------------------------- csv


def write_seed(tmp_path: Path, body: str = CSV_ROWS) -> Path:
    path = tmp_path / "BTCUSDT_1h.csv"
    path.write_text(CSV_HEADER + body, encoding="utf-8")
    return path


def test_the_still_forming_candle_is_dropped() -> None:
    from datetime import timedelta

    from aegis_core import utc_now

    def row(open_ms: int, close_ms: int, price: str) -> list[object]:
        return [open_ms, price, price, price, price, "10", close_ms, "0", 5, "0", "0", "0"]

    now = utc_now()
    closed_close = int((now - timedelta(hours=1)).timestamp() * 1000)
    open_close = int((now + timedelta(hours=1)).timestamp() * 1000)
    open_ms = int((now - timedelta(hours=2)).timestamp() * 1000)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                row(open_ms, closed_close, "100"),
                row(closed_close, open_close, "101"),
            ],
        )

    with BinanceKlineSource(client=make_client(handler)) as source:
        candles = source.fetch("BTC/USDT", Timeframe.H1, 500)

    assert [str(candle.close) for candle in candles] == ["100"]


def test_csv_source_resolves_the_expected_filename(tmp_path: Path) -> None:
    source = CsvCandleSource(tmp_path)

    assert source.path_for("btc-usdt", Timeframe.H1).name == "BTCUSDT_1h.csv"
    assert source.label == "csv"
    assert source.venue == "spot"


def test_csv_source_returns_the_most_recent_candles(tmp_path: Path) -> None:
    write_seed(tmp_path)
    source = CsvCandleSource(tmp_path)

    candles = source.fetch("BTC/USDT", Timeframe.H1, limit=2)

    assert len(candles) == 2
    assert [candle.open_time.hour for candle in candles] == [1, 2]


def test_csv_source_rejects_an_empty_file(tmp_path: Path) -> None:
    write_seed(tmp_path, body="")

    with pytest.raises(CollectorError, match="no candles"):
        CsvCandleSource(tmp_path).fetch("BTC/USDT", Timeframe.H1, 10)


def test_csv_source_propagates_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        CsvCandleSource(tmp_path).fetch("BTC/USDT", Timeframe.H1, 10)


def test_committed_seed_is_readable_through_the_source() -> None:
    source = CsvCandleSource(Path("data/seeds"))

    candles = source.fetch("BTC/USDT", Timeframe.H1, limit=5)

    assert len(candles) == 5
    assert candles[-1].symbol == "BTC/USDT"


# ------------------------------------------------------------------- binance


def make_client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://test")


def test_binance_payload_is_parsed() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[KLINE])

    with BinanceKlineSource(client=make_client(handler)) as source:
        candles = source.fetch("btc-usdt", Timeframe.H1, limit=1)

    (candle,) = candles
    assert candle.symbol == "BTC/USDT"
    assert candle.open_time == datetime(2024, 1, 1, tzinfo=UTC)
    assert candle.close_time == datetime(2024, 1, 1, 0, 59, 59, 999000, tzinfo=UTC)
    assert candle.open == Decimal("30000.00")
    assert candle.volume == Decimal("12.5")
    assert candle.trades == 42

    assert requests[0].url.path == "/api/v3/klines"
    assert requests[0].url.params["symbol"] == "BTCUSDT"
    assert requests[0].url.params["interval"] == "1h"
    assert requests[0].url.params["limit"] == "1"


def test_binance_http_error_becomes_collector_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"msg": "nope"})

    with (
        BinanceKlineSource(client=make_client(handler)) as source,
        pytest.raises(CollectorError, match="request failed"),
    ):
        source.fetch("BTC/USDT", Timeframe.H1, 1)


def test_binance_invalid_json_becomes_collector_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>nope</html>")

    with (
        BinanceKlineSource(client=make_client(handler)) as source,
        pytest.raises(CollectorError, match="invalid JSON"),
    ):
        source.fetch("BTC/USDT", Timeframe.H1, 1)


def test_binance_unexpected_payload_is_rejected() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -1121})

    with (
        BinanceKlineSource(client=make_client(handler)) as source,
        pytest.raises(CollectorError, match="unexpected klines payload"),
    ):
        source.fetch("BTC/USDT", Timeframe.H1, 1)


def test_binance_malformed_row_is_rejected() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[[1, "2"]])

    with (
        BinanceKlineSource(client=make_client(handler)) as source,
        pytest.raises(CollectorError, match="malformed kline"),
    ):
        source.fetch("BTC/USDT", Timeframe.H1, 1)


def test_binance_sends_end_time_so_a_backfill_can_page_backwards() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[KLINE])

    anchor = datetime(2024, 1, 1, tzinfo=UTC)
    with BinanceKlineSource(client=make_client(handler)) as source:
        source.fetch("BTC/USDT", Timeframe.H1, 1000, end_time=anchor)

    assert requests[0].url.params["endTime"] == str(int(anchor.timestamp() * 1000))
    assert requests[0].url.params["limit"] == "1000"


def test_binance_uses_the_configured_klines_path() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[KLINE])

    with BinanceKlineSource(client=make_client(handler), klines_path="/fapi/v1/klines") as source:
        source.fetch("BTC/USDT", Timeframe.H1, 1)

    assert requests[0].url.path == "/fapi/v1/klines"


def test_spot_klines_are_labelled_spot_by_default() -> None:
    source = BinanceKlineSource(client=make_client(lambda _: httpx.Response(200, json=[])))

    assert source.venue == "spot"
    source.close()


def test_the_futures_source_asks_the_futures_host_and_is_labelled_futures() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[KLINE])

    with BinanceFuturesSource(client=make_client(handler)) as source:
        source.fetch("BTC/USDT", Timeframe.H1, 1)

        assert source.venue == "futures"
    assert requests[0].url.path == FUTURES_KLINES_PATH


def test_an_unknown_venue_is_refused() -> None:
    with pytest.raises(ValueError, match="venue must be one of"):
        BinanceKlineSource(venue="margin")


def test_build_source_for_futures_returns_the_futures_source() -> None:
    source = build_source("binance", csv_dir="unused", venue="futures")

    assert isinstance(source, BinanceFuturesSource)
    assert source.venue == "futures"
    source.close()


def test_binance_does_not_close_a_client_it_does_not_own() -> None:
    client = make_client(lambda _: httpx.Response(200, json=[]))
    source = BinanceKlineSource(client=client)

    source.close()

    assert client.is_closed is False
    client.close()


# -------------------------------------------------------------------- factory


def test_build_source_returns_csv_for_paper_mode(tmp_path: Path) -> None:
    source = build_source("csv", csv_dir=tmp_path)

    assert isinstance(source, CsvCandleSource)
    assert source.data_dir == tmp_path


def test_build_source_returns_binance_for_exchange_mode() -> None:
    source = build_source("exchange", csv_dir="unused")

    assert isinstance(source, BinanceKlineSource)
    source.close()


def test_build_source_rejects_unknown_sources() -> None:
    with pytest.raises(ValueError, match="unknown market data source"):
        build_source("telepathy", csv_dir="unused")
