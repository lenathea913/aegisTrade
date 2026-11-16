"""Demo strategy: EMA fast/slow crossover.

This exists to exercise the pipeline (backtest -> risk -> paper), not to make money.
It trades one symbol, one position at a time, with a fixed quantity and a percentage
stop/target. Every signal is derived only from closed candles.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aegis_core import Candle, OrderIntent, OrderType, Side, Timeframe
from aegis_core.types import PositiveDecimal, normalise_symbol
from aegis_strategy.base import StrategyBase, ema

__all__ = ["EmaCrossParams", "EmaCrossStrategy"]

_HUNDRED = Decimal("100")


class EmaCrossParams(BaseModel):
    """Tunables. Percentages are percent units (``2`` = 2 %)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # TODO: revert to 12/26 after testing. Lowered to 3/5 so a quiet market still
    # produces crosses for an end-to-end run (signal -> risk -> paper -> Telegram).
    fast_period: int = Field(default=3, ge=2, le=200)
    slow_period: int = Field(default=5, ge=3, le=500)
    qty: PositiveDecimal = Decimal("0.1")
    stop_loss_pct: Decimal = Field(default=Decimal("2"), gt=0, lt=100)
    take_profit_pct: Decimal = Field(default=Decimal("4"), gt=0, le=1000)

    @model_validator(mode="after")
    def _fast_must_be_shorter(self) -> Self:
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be shorter than slow_period")
        return self


class EmaCrossStrategy(StrategyBase):
    """BUY when the fast EMA crosses above the slow EMA, SELL when it crosses below."""

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: Timeframe = Timeframe.H1,
        params: EmaCrossParams | None = None,
    ) -> None:
        self.symbol = normalise_symbol(symbol)
        self.timeframe = timeframe
        self.params = params or EmaCrossParams()

    @property
    def strategy_id(self) -> str:
        return "simple_ema_cross"

    @property
    def warmup_bars(self) -> int:
        # One extra bar so the previous EMAs exist for the cross comparison.
        return self.params.slow_period + 1

    def on_candle(self, candle: Candle, history: Sequence[Candle]) -> OrderIntent | None:
        if len(history) < self.warmup_bars:
            return None
        if candle.symbol != self.symbol:
            return None

        closes = [bar.close for bar in history]
        previous = closes[:-1]

        fast_now = ema(closes, self.params.fast_period)
        slow_now = ema(closes, self.params.slow_period)
        fast_prev = ema(previous, self.params.fast_period)
        slow_prev = ema(previous, self.params.slow_period)

        if fast_prev <= slow_prev and fast_now > slow_now:
            return self._intent(candle, Side.BUY, "crossed above")
        if fast_prev >= slow_prev and fast_now < slow_now:
            return self._intent(candle, Side.SELL, "crossed below")
        return None

    def _intent(self, candle: Candle, side: Side, event: str) -> OrderIntent:
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
                f"ema({self.params.fast_period}) {event} ema({self.params.slow_period}) "
                f"at close {entry}"
            ),
            created_at=candle.close_time,
        )
