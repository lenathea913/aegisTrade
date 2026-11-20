"""Demo strategy: EMA fast/slow crossover.

This exists to exercise the pipeline (backtest -> risk -> paper), not to make money.
It trades one symbol, one position at a time, with a fixed quantity and a percentage
stop/target. Every signal is derived only from closed candles.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aegis_core import Candle, OrderIntent, OrderType, Side, Timeframe
from aegis_core.types import PositiveDecimal, normalise_symbol
from aegis_strategy.base import StrategyBase, ema
from aegis_strategy.indicators import AdxTracker

__all__ = ["EmaCrossParams", "EmaCrossStrategy"]

_HUNDRED = Decimal("100")


class EmaCrossParams(BaseModel):
    """Tunables. Percentages are percent units (``2`` = 2 %)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fast_period: int = Field(default=50, ge=2, le=200)
    slow_period: int = Field(default=200, ge=3, le=500)
    qty: PositiveDecimal = Decimal("0.1")
    stop_loss_pct: Decimal = Field(default=Decimal("2"), gt=0, lt=100)
    take_profit_pct: Decimal = Field(default=Decimal("4"), gt=0, le=1000)

    #: ADX period for the regime filter.
    adx_period: int = Field(default=14, ge=2, le=100)
    #: Refuse to enter unless ADX is at least this value. A cross in a market below
    #: ~25 is usually noise: price is oscillating, not trending, and the strategy pays
    #: the spread and fees for a move that reverses. ``None`` disables the filter, which
    #: is what a baseline comparison run needs.
    adx_threshold: Decimal | None = Field(default=Decimal("25"), ge=0, le=100)

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
        # Memoised EMA per period. Recomputing the average over the whole history on
        # every bar is quadratic: fine for 500 bars, minutes for 11 000. The cache
        # holds only the last scalar and the bar count, never the history itself, and
        # it is reused only when the same series simply grew by one bar; anything
        # else (a new run, a different series) falls back to a fresh computation, so
        # the values are identical to recalculating from scratch.
        self._ema_cache: dict[int, tuple[int, Decimal, Decimal]] = {}
        # ADX is stateful by nature (Wilder smoothing), so it is tracked alongside the
        # history it belongs to and rebuilt whenever that history is not simply the
        # previous one plus a bar.
        self._adx = AdxTracker(self.params.adx_period)
        self._adx_seen = 0
        self._adx_last_time: datetime | None = None

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

        fast_prev, fast_now = self._ema_values(closes, self.params.fast_period)
        slow_prev, slow_now = self._ema_values(closes, self.params.slow_period)

        if fast_prev <= slow_prev and fast_now > slow_now:
            side, event = Side.BUY, "crossed above"
        elif fast_prev >= slow_prev and fast_now < slow_now:
            side, event = Side.SELL, "crossed below"
        else:
            return None

        if not self._trend_strong_enough(history):
            return None
        return self._intent(candle, side, event)

    def _trend_strong_enough(self, history: Sequence[Candle]) -> bool:
        """Whether ADX clears the configured floor at this bar.

        An index that does not exist yet (fewer than two ADX periods) cannot veto an
        entry: the filter needs history to speak, and blocking a signal because its
        *filter* has not warmed up would silently delete the first few crosses.
        """
        threshold = self.params.adx_threshold
        value = self._adx_value(history)
        if threshold is None or value is None:
            return True
        return value >= threshold

    def _adx_value(self, history: Sequence[Candle]) -> Decimal | None:
        """ADX over ``history``, fed incrementally when the series only grew."""
        continued = (
            self._adx_seen + 1 == len(history) and self._adx_last_time == history[-2].close_time
        )
        if not continued:
            self._adx = AdxTracker(self.params.adx_period)
            self._adx_seen = 0
            start = 0
        else:
            start = len(history) - 1
        for bar in history[start:]:
            self._adx.update(bar)
            self._adx_seen += 1
        self._adx_last_time = history[-1].close_time
        return self._adx.value

    def _ema_values(self, closes: Sequence[Decimal], period: int) -> tuple[Decimal, Decimal]:
        """``(previous, current)`` EMA over ``closes``; incremental when it can be."""
        alpha = Decimal(2) / Decimal(period + 1)
        cached = self._ema_cache.get(period)
        if cached is not None and cached[0] == len(closes) - 1 and cached[1] == closes[-2]:
            previous = cached[2]
            current = closes[-1] * alpha + previous * (Decimal(1) - alpha)
        else:
            current = ema(closes, period)
            previous = ema(closes[:-1], period) if len(closes) > period else current
        self._ema_cache[period] = (len(closes), closes[-1], current)
        return previous, current

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
