"""Mean reversion: buy the lower Bollinger band, sell the upper one.

The EMA cross is a trend follower and loses money when price oscillates without a
direction. This strategy is the complement: it assumes the opposite regime, that a
stretch away from the moving average is more likely to be faded than continued.

A band is "touched" when the previous close was still inside the band and the current
close reaches or crosses the line. Requiring the crossing (instead of ``close <=
lower``) matters: while price sits outside the band the second condition stays true on
every bar, which would fire an entry on every single candle of the move.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aegis_core import Candle, OrderIntent, OrderType, Side, Timeframe
from aegis_core.types import PositiveDecimal, normalise_symbol
from aegis_strategy.base import StrategyBase
from aegis_strategy.indicators import bollinger_bands

__all__ = ["BollingerParams", "BollingerReversionStrategy"]

_HUNDRED = Decimal("100")


class BollingerParams(BaseModel):
    """Tunables. Percentages are percent units (``2`` = 2 %)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    period: int = Field(default=20, ge=2, le=200)
    num_std: Decimal = Field(default=Decimal("2"), gt=0, le=10)
    qty: PositiveDecimal = Decimal("0.1")
    stop_loss_pct: Decimal = Field(default=Decimal("2"), gt=0, lt=100)
    take_profit_pct: Decimal = Field(default=Decimal("4"), gt=0, le=1000)

    @model_validator(mode="after")
    def _take_profit_must_clear_the_stop(self) -> Self:
        if self.take_profit_pct <= self.stop_loss_pct:
            raise ValueError("take_profit_pct must be greater than stop_loss_pct")
        return self


class BollingerReversionStrategy(StrategyBase):
    """BUY when price enters the lower band, SELL when it enters the upper band."""

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: Timeframe = Timeframe.H1,
        params: BollingerParams | None = None,
    ) -> None:
        self.symbol = normalise_symbol(symbol)
        self.timeframe = timeframe
        self.params = params or BollingerParams()

    @property
    def strategy_id(self) -> str:
        return "bollinger_reversion"

    @property
    def warmup_bars(self) -> int:
        # One extra bar so the previous bands exist for the touch comparison.
        return self.params.period + 1

    def on_candle(self, candle: Candle, history: Sequence[Candle]) -> OrderIntent | None:
        if len(history) < self.warmup_bars:
            return None
        if candle.symbol != self.symbol:
            return None

        closes = [bar.close for bar in history]
        bands_now = bollinger_bands(closes, self.params.period, self.params.num_std)
        bands_prev = bollinger_bands(closes[:-1], self.params.period, self.params.num_std)
        if bands_now is None or bands_prev is None:
            return None

        previous_close = closes[-2]
        if previous_close > bands_prev.lower and candle.close <= bands_now.lower:
            return self._intent(candle, Side.BUY, bands_now.lower, "lower")
        if previous_close < bands_prev.upper and candle.close >= bands_now.upper:
            return self._intent(candle, Side.SELL, bands_now.upper, "upper")
        return None

    def _intent(self, candle: Candle, side: Side, band: Decimal, which: str) -> OrderIntent:
        entry = candle.close
        if side is Side.BUY:
            stop_loss = entry * (Decimal(1) - self.params.stop_loss_pct / _HUNDRED)
            take_profit = entry * (Decimal(1) + self.params.take_profit_pct / _HUNDRED)
        else:
            stop_loss = entry * (Decimal(1) + self.params.stop_loss_pct / _HUNDRED)
            take_profit = entry * (Decimal(1) - self.params.take_profit_pct / _HUNDRED)

        return OrderIntent(
            symbol=candle.symbol,
            side=side,
            order_type=OrderType.MARKET,
            qty=self.params.qty,
            price=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy_id=self.strategy_id,
            reason=(
                f"close {entry} touched the {which} band {band} "
                f"({self.params.period}, {self.params.num_std} sd)"
            ),
            created_at=candle.close_time,
        )
