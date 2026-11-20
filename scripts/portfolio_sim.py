"""Portfolio simulation: ten symbols competing for three slots on one equity.

The single-symbol engine in ``aegis_backtest`` answers "does this symbol make money
alone". That is not the question a portfolio asks: with ``max_open_positions = 3`` and
one shared $10 000, an idea can be perfectly good and still never be entered because
two stronger ones already hold the slots, and the third position consumes the total
exposure room that sized the first two.

Rules reproduced here, in the same order the live path applies them:

* every entry is ranked by signal strength (ADX + distance beyond the slow EMA, the
  same function the runner uses), so slots go to the strongest ideas;
* at most ``max_open_positions`` positions at once, 20 % of equity per symbol and 60 %
  total; sizing is the smaller of the risk budget (0.5 % of equity at the stop), the
  remaining room under both caps and free cash;
* a signal fills at the **open of the next bar** with adverse slippage and taker fees;
  stops and targets are checked intrabar against the high/low, stop first;
* funding is charged on open notional at every 8-hour boundary (00:00, 08:00, 16:00);
* no pyramiding: a same-direction signal on a held symbol is ignored, an opposite one
  closes the position (and reverses only if a slot and room remain).
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from itertools import pairwise
from typing import overload

from aegis_backtest import BacktestTrade, EquityPoint, ExitReason
from aegis_core import MARGIN_UTILISATION, Candle, OrderIntent, Side
from aegis_metrics import PerformanceMetrics, compute_metrics
from aegis_strategy import EmaCrossParams, EmaCrossStrategy
from trading_runner.ranking import strength_from_context

__all__ = ["PortfolioResult", "PortfolioRules", "PortfolioSimulator", "run_portfolio"]

_BPS = Decimal("10000")
_HUNDRED = Decimal("100")
_ONE = Decimal("1")
_ZERO = Decimal("0")
_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True)
class PortfolioRules:
    """Capital, caps and costs for one portfolio run."""

    initial_equity: Decimal = Decimal("10000")
    max_open_positions: int = 3
    max_exposure_per_symbol_pct: Decimal = Decimal("20")
    max_total_exposure_pct: Decimal = Decimal("60")
    risk_per_trade_pct: Decimal = Decimal("0.5")
    taker_fee_bps: Decimal = Decimal("7")
    slippage_bps: Decimal = Decimal("10")
    funding_bps_per_8h: Decimal = Decimal("1")
    allow_short: bool = True


@dataclass
class _Position:
    symbol: str
    qty: Decimal  # signed: positive long, negative short
    entry_price: Decimal
    entry_time: datetime
    stop_loss: Decimal
    take_profit: Decimal
    margin: Decimal
    entry_fee: Decimal
    risk_amount: Decimal
    funding: Decimal = _ZERO

    @property
    def notional_at_entry(self) -> Decimal:
        return abs(self.qty) * self.entry_price


@dataclass(frozen=True)
class PortfolioResult:
    """What the shared book did, including how often a slot was the reason nothing happened."""

    rules: PortfolioRules
    symbols: tuple[str, ...]
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    metrics: PerformanceMetrics
    rejected_for_slots: int = 0
    rejected_for_room: int = 0
    skipped_pyramid: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None

    @property
    def final_equity(self) -> Decimal:
        return self.equity_curve[-1].equity if self.equity_curve else self.rules.initial_equity

    @property
    def total_return_pct(self) -> Decimal:
        return (self.final_equity / self.rules.initial_equity - _ONE) * _HUNDRED

    @property
    def total_fees(self) -> Decimal:
        return sum((trade.fee for trade in self.trades), _ZERO)

    @property
    def total_funding(self) -> Decimal:
        return sum((trade.funding for trade in self.trades), _ZERO)


class _Prefix(Sequence[Candle]):
    """A read-only view of ``data[:stop]`` so the strategy never copies the history."""

    __slots__ = ("_data", "_stop")

    def __init__(self, data: Sequence[Candle], stop: int) -> None:
        self._data = data
        self._stop = stop

    def __len__(self) -> int:
        return self._stop

    @overload
    def __getitem__(self, index: int) -> Candle: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Candle]: ...

    def __getitem__(self, index: int | slice) -> Candle | Sequence[Candle]:
        if isinstance(index, slice):
            start, stop, step = index.indices(self._stop)
            return self._data[start:stop:step]
        if index < 0:
            index += self._stop
        if not 0 <= index < self._stop:
            raise IndexError(index)
        return self._data[index]


def _with_slippage(price: Decimal, direction: int, slippage_bps: Decimal) -> Decimal:
    return price * (_ONE + slippage_bps / _BPS * Decimal(direction))


def _fee(price: Decimal, qty: Decimal, fee_bps: Decimal) -> Decimal:
    return price * abs(qty) * fee_bps / _BPS


def _periods_per_year(timeline: Sequence[datetime]) -> float:
    if len(timeline) < 2:
        return 365.0 * 24
    deltas = [(later - earlier).total_seconds() for earlier, later in pairwise(timeline)]
    median = statistics.median(deltas)
    return 365.0 * 24 * 3600 / median if median > 0 else 365.0 * 24


class PortfolioSimulator:
    """One shared book across every symbol in ``series``."""

    def __init__(
        self,
        series: Mapping[str, Sequence[Candle]],
        rules: PortfolioRules | None = None,
        params: EmaCrossParams | None = None,
        *,
        rank_by_strength: bool = True,
    ) -> None:
        self.rules = rules or PortfolioRules()
        #: Off reproduces the old symbol-alphabetical order, which is what the slot
        #: contention used to decide on. Kept so a run can show what the ranking is
        #: worth instead of asserting it.
        self._rank_by_strength = rank_by_strength
        self._candles = {symbol: list(bars) for symbol, bars in series.items() if bars}
        if not self._candles:
            raise ValueError("portfolio simulation needs at least one symbol with candles")
        self._timeframes = {bars[0].timeframe for bars in self._candles.values()}
        if len(self._timeframes) != 1:
            raise ValueError(f"all series must share one timeframe, got {self._timeframes}")
        self._timeframe = next(iter(self._timeframes))
        self._params = params or EmaCrossParams()
        self._bars: dict[str, dict[datetime, Candle]] = {
            symbol: {bar.open_time: bar for bar in bars} for symbol, bars in self._candles.items()
        }
        self._index = {
            symbol: {bar.open_time: position for position, bar in enumerate(bars)}
            for symbol, bars in self._candles.items()
        }
        self._strategies = {
            symbol: EmaCrossStrategy(symbol=symbol, timeframe=self._timeframe, params=self._params)
            for symbol in self._candles
        }
        self._timeline = sorted({moment for bars in self._bars.values() for moment in bars})

        self.cash = self.rules.initial_equity
        self.positions: dict[str, _Position] = {}
        self._pending: dict[str, OrderIntent] = {}
        self._marks: dict[str, Decimal] = {}
        self.trades: list[BacktestTrade] = []
        self.curve: list[EquityPoint] = []
        self.rejected_for_slots = 0
        self.rejected_for_room = 0
        self.skipped_pyramid = 0

    # ------------------------------------------------------------------ money

    def equity(self) -> Decimal:
        total = self.cash
        for position in self.positions.values():
            mark = self._marks.get(position.symbol, position.entry_price)
            total += position.margin + position.qty * (mark - position.entry_price)
        return total

    def total_exposure(self) -> Decimal:
        return sum(
            (
                abs(position.qty) * self._marks.get(position.symbol, position.entry_price)
                for position in self.positions.values()
            ),
            _ZERO,
        )

    # -------------------------------------------------------------- execution

    def _size(self, intent: OrderIntent, price: Decimal) -> Decimal:
        """Risk budget, exposure room and free cash: the smallest of the three."""
        risk_per_unit = abs(price - intent.stop_loss)
        if risk_per_unit <= 0 or price <= 0:
            return _ZERO
        equity = self.equity()
        risk_budget = equity * self.rules.risk_per_trade_pct / _HUNDRED
        qty_risk = risk_budget / risk_per_unit

        room = min(
            equity * self.rules.max_exposure_per_symbol_pct / _HUNDRED,
            equity * self.rules.max_total_exposure_pct / _HUNDRED - self.total_exposure(),
        )
        qty_room = room / price if room > 0 else _ZERO
        qty_cash = self.cash * MARGIN_UTILISATION / price
        qty = min(qty_risk, qty_room, qty_cash)
        return qty.quantize(_QUANTUM, rounding=ROUND_DOWN) if qty > 0 else _ZERO

    def _open(self, intent: OrderIntent, price: Decimal, qty: Decimal, moment: datetime) -> None:
        signed = qty if intent.side is Side.BUY else -qty
        notional = qty * price
        fee = _fee(price, qty, self.rules.taker_fee_bps)
        self.cash -= notional + fee
        self.positions[intent.symbol] = _Position(
            symbol=intent.symbol,
            qty=signed,
            entry_price=price,
            entry_time=moment,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            margin=notional,
            entry_fee=fee,
            risk_amount=abs(price - intent.stop_loss) * qty,
        )

    def _close(self, symbol: str, price: Decimal, moment: datetime, reason: ExitReason) -> None:
        position = self.positions.pop(symbol)
        fee = _fee(price, position.qty, self.rules.taker_fee_bps)
        direction = _ONE if position.qty > 0 else -_ONE
        gross = (price - position.entry_price) * abs(position.qty) * direction
        self.cash += position.margin + gross - fee
        net = gross - position.entry_fee - fee - position.funding
        self.trades.append(
            BacktestTrade(
                symbol=symbol,
                side=Side.BUY if direction > 0 else Side.SELL,
                qty=abs(position.qty),
                entry_time=position.entry_time,
                entry_price=position.entry_price,
                exit_time=moment,
                exit_price=price,
                fee=position.entry_fee + fee,
                funding=position.funding,
                gross_pnl=gross,
                net_pnl=net,
                r_multiple=(net / position.risk_amount if position.risk_amount > 0 else None),
                exit_reason=reason,
                strategy_id="simple_ema_cross",
            )
        )

    def _fill(self, intent: OrderIntent, candle: Candle) -> None:
        price = _with_slippage(candle.open, intent.side.sign, self.rules.slippage_bps)
        held = self.positions.get(intent.symbol)
        if held is not None:
            same_direction = (held.qty > 0) == (intent.side is Side.BUY)
            if same_direction:
                self.skipped_pyramid += 1
                return
            # An opposite signal is an exit; the reverse leg has to earn its own slot
            # and room after the close.
            self._close(intent.symbol, price, candle.open_time, ExitReason.SIGNAL)

        if intent.side is Side.SELL and not self.rules.allow_short:
            return
        if len(self.positions) >= self.rules.max_open_positions:
            self.rejected_for_slots += 1
            return

        qty = self._size(intent, price)
        if qty <= 0:
            self.rejected_for_room += 1
            return
        self._open(intent, price, qty, candle.open_time)

    def _fund(self, candle: Candle) -> None:
        position = self.positions.get(candle.symbol)
        rate = self.rules.funding_bps_per_8h
        if position is None or rate == 0:
            return
        if candle.open_time.hour % 8 != 0 or candle.open_time.minute != 0:
            return
        direction = _ONE if position.qty > 0 else -_ONE
        amount = abs(position.qty) * candle.open * rate / _BPS * direction
        self.cash -= amount
        position.funding += amount

    def _check_exits(self, candle: Candle) -> None:
        position = self.positions.get(candle.symbol)
        if position is None:
            return
        slippage = self.rules.slippage_bps
        if position.qty > 0:
            if candle.low <= position.stop_loss:
                price = _with_slippage(position.stop_loss, -1, slippage)
                self._close(candle.symbol, price, candle.open_time, ExitReason.STOP_LOSS)
            elif candle.high >= position.take_profit:
                price = _with_slippage(position.take_profit, -1, slippage)
                self._close(candle.symbol, price, candle.open_time, ExitReason.TAKE_PROFIT)
        else:
            if candle.high >= position.stop_loss:
                price = _with_slippage(position.stop_loss, 1, slippage)
                self._close(candle.symbol, price, candle.open_time, ExitReason.STOP_LOSS)
            elif candle.low <= position.take_profit:
                price = _with_slippage(position.take_profit, 1, slippage)
                self._close(candle.symbol, price, candle.open_time, ExitReason.TAKE_PROFIT)

    def _schedule(self, symbol: str, intent: OrderIntent) -> None:
        """Offer a candidate at the bar close, ranked against the other symbols."""
        held = self.positions.get(symbol)
        if held is not None and (held.qty > 0) == (intent.side is Side.BUY):
            self.skipped_pyramid += 1
            return
        if intent.side is Side.SELL and not self.rules.allow_short:
            return
        # A reverse frees its own slot, so only a genuinely new idea competes.
        occupied = len(self.positions) - (0 if held is None else 1)
        slots_used = occupied + len(self._pending)
        if held is None and slots_used >= self.rules.max_open_positions:
            self.rejected_for_slots += 1
            return
        self._pending[symbol] = intent

    # ------------------------------------------------------------------- loop

    def run(self) -> PortfolioResult:
        for moment in self._timeline:
            bars = {
                symbol: self._bars[symbol][moment]
                for symbol in self._candles
                if moment in self._bars[symbol]
            }

            for candle in bars.values():
                self._marks[candle.symbol] = candle.open
                self._fund(candle)

            for symbol in [s for s in self._pending if s in bars]:
                intent = self._pending.pop(symbol)
                self._fill(intent, bars[symbol])

            for candle in bars.values():
                self._check_exits(candle)

            candidates: list[tuple[Decimal, str, OrderIntent]] = []
            for symbol, candle in bars.items():
                strategy = self._strategies[symbol]
                history = _Prefix(self._candles[symbol], self._index[symbol][moment] + 1)
                intent = strategy.on_candle(candle, history)
                if intent is None:
                    continue
                strength = strength_from_context(
                    {
                        "adx": strategy.last_adx,
                        "ema_distance_pct": strategy.last_ema_distance_pct,
                    }
                )
                candidates.append((strength, symbol, intent))
            if self._rank_by_strength:
                # Strongest trend first; the ticker is only a tie-break.
                candidates.sort(key=lambda item: (-item[0], item[1]))
            else:
                candidates.sort(key=lambda item: item[1])
            for _, symbol, intent in candidates:
                self._schedule(symbol, intent)

            for candle in bars.values():
                self._marks[candle.symbol] = candle.close
            self.curve.append(
                EquityPoint(
                    timestamp=moment,
                    equity=self.equity(),
                    cash=self.cash,
                    position_qty=Decimal(len(self.positions)),
                )
            )

        self._close_everything()
        return self._result()

    def _close_everything(self) -> None:
        if not self.positions:
            return
        last_moment = self._timeline[-1]
        for symbol in list(self.positions):
            mark = self._marks.get(symbol)
            if mark is None:
                continue
            self._close(
                symbol,
                _with_slippage(
                    mark, -1 if self.positions[symbol].qty > 0 else 1, self.rules.slippage_bps
                ),
                last_moment,
                ExitReason.END_OF_DATA,
            )
        self.curve.append(
            EquityPoint(
                timestamp=last_moment,
                equity=self.cash,
                cash=self.cash,
                position_qty=_ZERO,
            )
        )

    def _result(self) -> PortfolioResult:
        metrics = compute_metrics(
            [trade.net_pnl for trade in self.trades],
            [point.equity for point in self.curve],
            [float(trade.r_multiple) for trade in self.trades if trade.r_multiple is not None],
            _periods_per_year(self._timeline),
        )
        return PortfolioResult(
            rules=self.rules,
            symbols=tuple(sorted(self._candles)),
            trades=tuple(self.trades),
            equity_curve=tuple(self.curve),
            metrics=metrics,
            rejected_for_slots=self.rejected_for_slots,
            rejected_for_room=self.rejected_for_room,
            skipped_pyramid=self.skipped_pyramid,
            started_at=self._timeline[0],
            ended_at=self._timeline[-1],
        )


def run_portfolio(
    series: Mapping[str, Sequence[Candle]],
    rules: PortfolioRules | None = None,
    params: EmaCrossParams | None = None,
    *,
    rank_by_strength: bool = True,
) -> PortfolioResult:
    """Simulate every symbol in ``series`` on one book, in one call."""
    return PortfolioSimulator(series, rules, params, rank_by_strength=rank_by_strength).run()
