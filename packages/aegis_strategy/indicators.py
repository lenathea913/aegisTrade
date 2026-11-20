"""Indicators a strategy can use as a filter.

Two things live here, both pure maths on closed candles:

* ``AdxTracker`` — Wilder's Average Directional Index, updated **one bar at a time**.
  The backtest hands the strategy the whole history on every bar, so recomputing the
  index from scratch would be quadratic exactly like the EMA was; the tracker keeps
  the smoothed sums instead and is therefore O(1) per bar.
* ``bollinger_bands`` — the usual SMA ± ``num_std`` standard deviations over a window.

Neither function can see a future bar: callers pass history up to and including the
bar that just closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from aegis_core import Candle

__all__ = ["AdxTracker", "BollingerBands", "adx", "bollinger_bands"]

_HUNDRED = Decimal("100")
_ZERO = Decimal("0")


class AdxTracker:
    """Wilder's ADX for one series, fed bar by bar.

    The index answers "is this market trending or choppy?". It is built from the
    directional movement (+DM / -DM) and true range, each smoothed with Wilder's
    recursion, then from the smoothed directional indices (+DI / -DI) and their
    normalised spread (DX). ADX is the smoothed DX.

    ``value`` is ``None`` until two full periods of bars have been seen — the index
    simply does not exist before that, and a caller must decide what "unknown" means
    instead of reading a made-up number.
    """

    def __init__(self, period: int = 14) -> None:
        if period < 2:
            raise ValueError("ADX period must be at least 2")
        self.period = period
        self._bars = 0
        self._prev_high: Decimal | None = None
        self._prev_low: Decimal | None = None
        self._prev_close: Decimal | None = None
        self._true_range = _ZERO
        self._plus_dm = _ZERO
        self._minus_dm = _ZERO
        self._dx_sum = _ZERO
        self._dx_count = 0
        self._adx: Decimal | None = None

    @property
    def value(self) -> Decimal | None:
        """Current ADX, or ``None`` while the warmup is still accumulating."""
        return self._adx

    def update(self, candle: Candle) -> None:
        """Fold one more closed bar into the index."""
        if self._prev_close is None:
            self._remember(candle)
            self._bars = 1
            return

        assert self._prev_high is not None and self._prev_low is not None
        true_range = max(
            candle.high - candle.low,
            abs(candle.high - self._prev_close),
            abs(candle.low - self._prev_close),
        )
        up_move = candle.high - self._prev_high
        down_move = self._prev_low - candle.low
        plus_dm = up_move if up_move > down_move and up_move > 0 else _ZERO
        minus_dm = down_move if down_move > up_move and down_move > 0 else _ZERO

        self._remember(candle)
        self._bars += 1

        period = Decimal(self.period)
        if self._bars <= self.period + 1:
            # Wilder seeds the smoothing with a plain sum of the first `period` values.
            self._true_range += true_range
            self._plus_dm += plus_dm
            self._minus_dm += minus_dm
        else:
            self._true_range = self._true_range - self._true_range / period + true_range
            self._plus_dm = self._plus_dm - self._plus_dm / period + plus_dm
            self._minus_dm = self._minus_dm - self._minus_dm / period + minus_dm

        if self._bars <= self.period or self._true_range <= 0:
            return

        di_plus = _HUNDRED * self._plus_dm / self._true_range
        di_minus = _HUNDRED * self._minus_dm / self._true_range
        total = di_plus + di_minus
        if total <= 0:
            return
        dx = _HUNDRED * abs(di_plus - di_minus) / total

        if self._adx is None:
            self._dx_sum += dx
            self._dx_count += 1
            if self._dx_count >= self.period:
                self._adx = self._dx_sum / Decimal(self.period)
        else:
            self._adx = (self._adx * (period - 1) + dx) / period

    def _remember(self, candle: Candle) -> None:
        self._prev_high = candle.high
        self._prev_low = candle.low
        self._prev_close = candle.close


def adx(candles: Sequence[Candle], period: int = 14) -> Decimal | None:
    """ADX of a whole series, computed by replaying :class:`AdxTracker` over it."""
    tracker = AdxTracker(period)
    for candle in candles:
        tracker.update(candle)
    return tracker.value


@dataclass(frozen=True)
class BollingerBands:
    """The three lines of a Bollinger band at one bar."""

    lower: Decimal
    middle: Decimal
    upper: Decimal


def bollinger_bands(
    closes: Sequence[Decimal],
    period: int = 20,
    num_std: Decimal = Decimal("2"),
) -> BollingerBands | None:
    """SMA and ``num_std`` population standard deviations of the last ``period`` closes.

    Returns ``None`` while there are fewer than ``period`` values, so a caller cannot
    accidentally treat an incomplete window as a band.
    """
    if period < 2:
        raise ValueError("Bollinger period must be at least 2")
    if num_std <= 0:
        raise ValueError("num_std must be positive")
    if len(closes) < period:
        return None

    window = closes[-period:]
    middle = sum(window, _ZERO) / Decimal(period)
    variance = sum(((value - middle) ** 2 for value in window), _ZERO) / Decimal(period)
    offset = variance.sqrt() * num_std
    return BollingerBands(lower=middle - offset, middle=middle, upper=middle + offset)
