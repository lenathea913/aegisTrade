"""OHLCV candle: the unit of market data shared by collector, strategy and backtest.

The candle carries its own ``close_time``, and strategies only ever see candles that
have already closed. That is the mechanical basis for "no look-ahead": a signal
derived from a candle is timestamped after that candle's close, so it can only be
executed on a later bar.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from aegis_core.base import StrictModel
from aegis_core.enums import Timeframe
from aegis_core.types import NonNegativeDecimal, PositiveDecimal, SymbolStr, UtcDatetime

__all__ = ["Candle"]


class Candle(StrictModel):
    """One closed OHLCV bar."""

    symbol: SymbolStr
    timeframe: Timeframe
    open_time: UtcDatetime
    close_time: UtcDatetime
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: NonNegativeDecimal = Decimal("0")
    trades: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_ohlc(self) -> Self:
        if self.close_time <= self.open_time:
            raise ValueError("close_time must be after open_time")
        if self.high < self.low:
            raise ValueError("high must be greater than or equal to low")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("open/close must lie within the high/low range")
        return self

    @property
    def range(self) -> Decimal:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open
