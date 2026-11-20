"""Candle sources.

Two implementations, one protocol:

* ``CsvCandleSource`` — paper/dev mode. Reads committed seed data, no network, so a
  fresh clone can run the whole pipeline offline.
* ``BinanceKlineSource`` — real OHLCV from Binance's public klines endpoint. Public
  data needs no API key; the client is injectable so tests use a mock transport and
  nothing hits the network.

Sources only *fetch*. Persisting, retrying and lag accounting belong to the
collector, which keeps this module free of database concerns.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import httpx

from aegis_backtest import load_candles_csv
from aegis_core import Candle, Timeframe, utc_now

__all__ = [
    "BINANCE_MAINNET_BASE_URL",
    "BinanceKlineSource",
    "CandleSource",
    "CollectorError",
    "CsvCandleSource",
    "build_source",
]


#: Public REST hosts. Klines need no credentials, but the host still follows the
#: exchange environment so a testnet run never mixes prices with mainnet.
BINANCE_MAINNET_BASE_URL = "https://api.binance.com"
BINANCE_TESTNET_BASE_URL = "https://testnet.binance.vision"


class CollectorError(RuntimeError):
    """A source could not produce usable candles."""


class CandleSource(Protocol):
    """Anything that can produce recent closed candles for a symbol/timeframe."""

    @property
    def label(self) -> str:
        """Short tag stored in ``market.candles.source`` (``csv`` / ``exchange``)."""

    def fetch(self, symbol: str, timeframe: Timeframe, limit: int) -> Sequence[Candle]:
        """Return up to ``limit`` most recent closed candles, oldest first."""


class CsvCandleSource:
    """Serves seed data from ``data_dir`` using ``{base}_{timeframe}.csv`` names."""

    def __init__(self, data_dir: Path | str, path_template: str = "{base}_{timeframe}.csv") -> None:
        self.data_dir = Path(data_dir)
        self.path_template = path_template

    @property
    def label(self) -> str:
        return "csv"

    def path_for(self, symbol: str, timeframe: Timeframe) -> Path:
        base = symbol.replace("/", "").replace("-", "").upper()
        return self.data_dir / self.path_template.format(base=base, timeframe=timeframe.value)

    def fetch(self, symbol: str, timeframe: Timeframe, limit: int) -> Sequence[Candle]:
        path = self.path_for(symbol, timeframe)
        candles = load_candles_csv(path, symbol=symbol, timeframe=timeframe)
        if not candles:
            raise CollectorError(f"{path} contains no candles")
        return candles[-limit:] if limit > 0 else candles


#: Binance interval strings for the timeframes the domain supports.
_BINANCE_INTERVALS: Mapping[Timeframe, str] = {
    Timeframe.M1: "1m",
    Timeframe.M5: "5m",
    Timeframe.M15: "15m",
    Timeframe.H1: "1h",
    Timeframe.H4: "4h",
    Timeframe.D1: "1d",
}


class BinanceKlineSource:
    """Public spot klines from ``/api/v3/klines`` (read-only, no credentials)."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.binance.com",
        klines_path: str = "/api/v3/klines",
        client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # Spot uses ``/api/v3/klines``; USDT-M futures use ``/fapi/v1/klines``. The
        # path is a parameter so a futures backfill reads the contract it trades
        # instead of spot prices that merely look similar.
        self._klines_path = klines_path
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    @property
    def label(self) -> str:
        return "exchange"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> BinanceKlineSource:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @staticmethod
    def exchange_symbol(symbol: str) -> str:
        return symbol.replace("/", "").replace("-", "").upper()

    def fetch(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
        *,
        end_time: datetime | None = None,
    ) -> Sequence[Candle]:
        """Latest closed candles, or those at/before ``end_time`` when given.

        ``end_time`` is what makes pagination possible: each page is anchored just
        before the oldest bar already received, so a caller can walk backwards
        through months of history without overlap or gaps.
        """
        params: dict[str, str | int] = {
            "symbol": self.exchange_symbol(symbol),
            "interval": _BINANCE_INTERVALS[timeframe],
            "limit": max(1, min(limit, 1000)),
        }
        if end_time is not None:
            params["endTime"] = int(end_time.timestamp() * 1000)
        try:
            response = self._client.get(f"{self._base_url}{self._klines_path}", params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise CollectorError(f"binance klines request failed: {exc}") from exc
        except ValueError as exc:
            raise CollectorError(f"binance returned invalid JSON: {exc}") from exc

        candles = self._parse(payload, symbol=symbol, timeframe=timeframe)
        # Binance includes the candle that is still forming; its close_time is in the
        # future. Storing it would feed an unfinished bar to the strategies (and report
        # a negative lag), so only closed candles leave this source.
        now = utc_now()
        closed = [candle for candle in candles if candle.close_time <= now]
        if not closed:
            raise CollectorError(
                f"binance returned no closed candles for {symbol} {timeframe.value}"
            )
        return closed

    @staticmethod
    def _parse(payload: Any, *, symbol: str, timeframe: Timeframe) -> list[Candle]:
        if not isinstance(payload, list):
            raise CollectorError(f"unexpected klines payload: {type(payload).__name__}")

        candles: list[Candle] = []
        try:
            for row in payload:
                candles.append(
                    Candle(
                        symbol=symbol,
                        timeframe=timeframe,
                        open_time=datetime.fromtimestamp(row[0] / 1000, tz=UTC),
                        close_time=datetime.fromtimestamp(row[6] / 1000, tz=UTC),
                        open=Decimal(str(row[1])),
                        high=Decimal(str(row[2])),
                        low=Decimal(str(row[3])),
                        close=Decimal(str(row[4])),
                        volume=Decimal(str(row[5])),
                        trades=int(row[8]) if len(row) > 8 else None,
                    )
                )
        except (IndexError, TypeError, ValueError) as exc:
            raise CollectorError(f"malformed kline row for {symbol} {timeframe}: {exc}") from exc

        return sorted(candles, key=lambda candle: candle.open_time)


def build_source(
    source: str,
    *,
    csv_dir: Path | str,
    timeout_seconds: float = 10.0,
    base_url: str = BINANCE_MAINNET_BASE_URL,
) -> CandleSource:
    """Factory used by the CLI and the paper stack.

    ``binance`` and ``exchange`` select the live klines source; only ``csv`` is the
    offline seed path. An unknown value fails loudly instead of falling back to seeds,
    because silently trading last year's prices is worse than refusing to start.
    """
    if source == "csv":
        return CsvCandleSource(csv_dir)
    if source in {"exchange", "binance"}:
        return BinanceKlineSource(
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )
    raise ValueError(f"unknown market data source: {source!r}")
