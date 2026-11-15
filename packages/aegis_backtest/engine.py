"""Event-driven backtest engine.

Timing (the part that decides whether a backtest is honest):

* A strategy sees bar *i* only **after it closed**, and the decision is timestamped
  ``candle.close_time``. There is no way to trade that bar's price.
* The resulting order fills at the open of bar ``i + 1 + latency_bars``, with adverse
  slippage and taker fees; a LIMIT order only fills if its price is touched and pays
  maker fees.
* Stop loss and take profit are evaluated intrabar against high/low, with the stop
  taking priority when both levels are crossed in the same bar (pessimistic).
* Funding is charged on open notional whenever a bar opens on an 8-hour boundary
  (00:00, 08:00, 16:00 UTC).

One position at a time, no pyramiding: a same-direction signal is ignored, an
opposite one closes the position (and only opens the reverse leg when
``allow_short`` is enabled).
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from itertools import pairwise

from aegis_backtest.config import BacktestConfig
from aegis_backtest.models import BacktestResult, BacktestTrade, EquityPoint, ExitReason
from aegis_core import Candle, OrderIntent, OrderType, Side, Timeframe
from aegis_metrics import compute_metrics
from aegis_strategy import StrategyBase

__all__ = ["run"]

_BPS = Decimal("10000")
_ONE = Decimal("1")
_ZERO = Decimal("0")


def _with_slippage(price: Decimal, direction: int, slippage_bps: Decimal) -> Decimal:
    """Adverse fill: buying pays up (direction +1), selling receives less (-1)."""
    return price * (_ONE + slippage_bps / _BPS * Decimal(direction))


def _fee(price: Decimal, qty: Decimal, fee_bps: Decimal) -> Decimal:
    return price * qty * fee_bps / _BPS


def _periods_per_year(candles: Sequence[Candle]) -> float:
    if len(candles) < 2:
        return 365.0 * 24
    deltas = [
        (later.open_time - earlier.open_time).total_seconds()
        for earlier, later in pairwise(candles)
    ]
    median_seconds = statistics.median(deltas)
    if median_seconds <= 0:
        return 365.0 * 24
    return 365.0 * 24 * 3600 / median_seconds


class _Simulator:
    """Single-symbol, single-position cash accounting."""

    def __init__(self, config: BacktestConfig, strategy_id: str, symbol: str, timeframe: Timeframe):
        self.config = config
        self.strategy_id = strategy_id
        self.symbol = symbol
        self.timeframe = timeframe
        self.cash: Decimal = config.initial_equity
        self.qty: Decimal = _ZERO
        self.entry_price: Decimal | None = None
        self.entry_time: datetime | None = None
        self.entry_fee: Decimal = _ZERO
        self.funding: Decimal = _ZERO
        self.stop_loss: Decimal | None = None
        self.take_profit: Decimal | None = None
        self.risk_amount: Decimal = _ZERO
        self.trades: list[BacktestTrade] = []
        self.curve: list[EquityPoint] = []

    @property
    def is_open(self) -> bool:
        return self.qty != 0

    def equity(self, mark: Decimal) -> Decimal:
        return self.cash + self.qty * mark

    # ---------------------------------------------------------------- execution

    def _fill_qty(self, intent: OrderIntent) -> Decimal:
        ratio = self.config.partial_fill_ratio
        return intent.qty if ratio is None else intent.qty * ratio

    def execute(self, intent: OrderIntent, candle: Candle) -> None:
        """Fill a scheduled intent on ``candle``, if the market allows it."""
        taker = self.config.taker_fee_bps
        maker = self.config.maker_fee_bps

        if intent.order_type is OrderType.LIMIT:
            limit = intent.price
            if limit is None or not (candle.low <= limit <= candle.high):
                return  # resting order never traded: no fill, no cost
            fill_price = limit
            fee_bps = maker
        else:
            fill_price = _with_slippage(candle.open, intent.side.sign, self.config.slippage_bps)
            fee_bps = taker

        qty = self._fill_qty(intent)
        if qty <= 0:
            return

        if self.is_open:
            same_direction = (self.qty > 0 and intent.side is Side.BUY) or (
                self.qty < 0 and intent.side is Side.SELL
            )
            if same_direction:
                return  # no pyramiding in this engine
            self.close(fill_price, candle.open_time, ExitReason.SIGNAL, taker)
            if not self.config.allow_short:
                return

        if intent.side is Side.SELL and not self.config.allow_short:
            return

        self.open(intent, fill_price, qty, candle.open_time, fee_bps)

    def open(
        self,
        intent: OrderIntent,
        fill_price: Decimal,
        qty: Decimal,
        timestamp: datetime,
        fee_bps: Decimal,
    ) -> None:
        signed_qty = qty if intent.side is Side.BUY else -qty
        fee = _fee(fill_price, qty, fee_bps)

        self.cash -= signed_qty * fill_price + fee
        self.qty = signed_qty
        self.entry_price = fill_price
        self.entry_time = timestamp
        self.entry_fee = fee
        self.funding = _ZERO
        self.stop_loss = intent.stop_loss
        self.take_profit = intent.take_profit
        self.risk_amount = abs(fill_price - intent.stop_loss) * qty

    def close(
        self,
        price: Decimal,
        timestamp: datetime,
        reason: ExitReason,
        fee_bps: Decimal,
    ) -> None:
        if not self.is_open or self.entry_price is None or self.entry_time is None:
            raise RuntimeError("cannot close a flat position")

        qty = self.qty
        abs_qty = abs(qty)
        direction = 1 if qty > 0 else -1
        fee = _fee(price, abs_qty, fee_bps)

        self.cash += qty * price - fee
        gross = (price - self.entry_price) * abs_qty * Decimal(direction)
        net = gross - self.entry_fee - fee - self.funding

        self.trades.append(
            BacktestTrade(
                symbol=self.symbol,
                side=Side.BUY if direction > 0 else Side.SELL,
                qty=abs_qty,
                entry_time=self.entry_time,
                entry_price=self.entry_price,
                exit_time=timestamp,
                exit_price=price,
                fee=self.entry_fee + fee,
                funding=self.funding,
                gross_pnl=gross,
                net_pnl=net,
                r_multiple=net / self.risk_amount if self.risk_amount > 0 else None,
                exit_reason=reason,
                strategy_id=self.strategy_id,
            )
        )

        self.qty = _ZERO
        self.entry_price = None
        self.entry_time = None
        self.entry_fee = _ZERO
        self.funding = _ZERO
        self.stop_loss = None
        self.take_profit = None
        self.risk_amount = _ZERO

    # ------------------------------------------------------------------ events

    def accrue_funding(self, candle: Candle) -> None:
        if not self.is_open:
            return
        rate = self.config.funding_rate_bps_per_8h
        if rate == 0:
            return
        if candle.open_time.hour % 8 != 0 or candle.open_time.minute != 0:
            return

        direction = _ONE if self.qty > 0 else -_ONE
        amount = abs(self.qty) * candle.open * rate / _BPS * direction
        self.cash -= amount
        self.funding += amount

    def check_exits(self, candle: Candle) -> None:
        """Stop first, then target: if both are crossed, assume the worse one."""
        if not self.is_open:
            return
        slippage = self.config.slippage_bps
        taker = self.config.taker_fee_bps

        if self.qty > 0:
            if self.stop_loss is not None and candle.low <= self.stop_loss:
                price = _with_slippage(self.stop_loss, -1, slippage)
                self.close(price, candle.open_time, ExitReason.STOP_LOSS, taker)
            elif self.take_profit is not None and candle.high >= self.take_profit:
                price = _with_slippage(self.take_profit, -1, slippage)
                self.close(price, candle.open_time, ExitReason.TAKE_PROFIT, taker)
        else:
            if self.stop_loss is not None and candle.high >= self.stop_loss:
                price = _with_slippage(self.stop_loss, 1, slippage)
                self.close(price, candle.open_time, ExitReason.STOP_LOSS, taker)
            elif self.take_profit is not None and candle.low <= self.take_profit:
                price = _with_slippage(self.take_profit, 1, slippage)
                self.close(price, candle.open_time, ExitReason.TAKE_PROFIT, taker)

    def mark(self, candle: Candle) -> None:
        self.curve.append(
            EquityPoint(
                timestamp=candle.close_time,
                equity=self.equity(candle.close),
                cash=self.cash,
                position_qty=self.qty,
            )
        )


def _validate(candles: Sequence[Candle]) -> None:
    if not candles:
        raise ValueError("backtest needs at least one candle")
    symbols = {candle.symbol for candle in candles}
    timeframes = {candle.timeframe for candle in candles}
    if len(symbols) != 1:
        raise ValueError(f"all candles must share one symbol, got {sorted(symbols)}")
    if len(timeframes) != 1:
        raise ValueError(f"all candles must share one timeframe, got {sorted(timeframes)}")
    for earlier, later in pairwise(candles):
        if later.open_time <= earlier.open_time:
            raise ValueError("candles must be strictly increasing by open_time")


def run(
    strategy: StrategyBase,
    data: Sequence[Candle],
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Replay ``data`` through ``strategy`` and return trades, curve and metrics.

    The strategy never sees a future bar, and no order can fill on the bar that
    produced its signal.
    """
    candles = list(data)
    _validate(candles)
    cfg = config or BacktestConfig()

    sim = _Simulator(cfg, strategy.strategy_id, candles[0].symbol, candles[0].timeframe)
    pending: tuple[OrderIntent, int] | None = None

    for index, candle in enumerate(candles):
        sim.accrue_funding(candle)

        if pending is not None and index >= pending[1]:
            sim.execute(pending[0], candle)
            pending = None

        sim.check_exits(candle)

        if pending is None:
            intent = strategy.on_candle(candle, candles[: index + 1])
            if intent is not None:
                pending = (intent, index + 1 + cfg.latency_bars)

        sim.mark(candle)

    if sim.is_open and cfg.close_at_end:
        last = candles[-1]
        sim.close(
            _with_slippage(last.close, -1 if sim.qty > 0 else 1, cfg.slippage_bps),
            last.close_time,
            ExitReason.END_OF_DATA,
            cfg.taker_fee_bps,
        )
        closed_point = sim.curve[-1]
        sim.curve[-1] = EquityPoint(
            timestamp=closed_point.timestamp,
            equity=sim.cash,
            cash=sim.cash,
            position_qty=_ZERO,
        )

    metrics = compute_metrics(
        [trade.net_pnl for trade in sim.trades],
        [point.equity for point in sim.curve],
        [float(trade.r_multiple) for trade in sim.trades if trade.r_multiple is not None],
        _periods_per_year(candles),
    )

    return BacktestResult(
        strategy_id=strategy.strategy_id,
        symbol=candles[0].symbol,
        timeframe=candles[0].timeframe,
        config=cfg,
        trades=tuple(sim.trades),
        equity_curve=tuple(sim.curve),
        metrics=metrics,
        started_at=candles[0].open_time,
        ended_at=candles[-1].close_time,
    )
