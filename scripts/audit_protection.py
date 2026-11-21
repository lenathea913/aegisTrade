"""Audit one closed position: did its protection execute where it claims?

``audit_lookahead.py`` proves the *entry* did not peek. This script does the same for
the **exit**, which is the half that can lose money silently: a stop that never fired, a
"stop" priced better than the bar ever traded, or an exit booked against a candle the
monitor could not have seen yet.

It reads only rows the platform wrote:

* the entry order - side, fill price/time, and the ``stop_loss``/``take_profit`` the
  risk gate approved;
* the closing order - ``strategy_id='position_monitor'`` and the reason string the
  monitor wrote (``stop_loss`` or ``take_profit`` touched);
* the closing trade - the price and quantity that were actually booked;
* the exit candle - the newest stored bar that had **closed** when the exit was decided.

and re-derives the decision itself, printing the numbers side by side:

* was the level really breached by that bar?
* did the bar's range actually reach the level (not just the close)?
* is the fill inside the bar's range, i.e. not better than the best price that bar
  offered?
* was the fill booked after that bar closed (no acting on a forming candle)?
* on the paper broker, is the fill exactly the monitor's reference price moved by the
  configured adverse slippage?

Usage::

    python scripts/audit_protection.py                 # newest protective exit
    python scripts/audit_protection.py --index 2       # the one before it
    python scripts/audit_protection.py --symbol ETH/USDT --venue futures

Exit codes: ``0`` every check passed, ``1`` a check contradicts the rules, ``2`` there
is nothing to audit (no closed position, or no candle to check it against).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from aegis_config import load_settings
from aegis_core import OrderStatus, Side, Timeframe
from aegis_db import Candle as CandleRow
from aegis_db import (
    OrderRecord,
    TradeRecord,
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)

__all__ = ["main"]

EXIT_STRATEGY = "position_monitor"
_BPS = Decimal("10000")
_ZERO = Decimal("0")
#: Quantities are venue-rounded; this is the same epsilon the monitor uses.
_QTY_EPSILON = Decimal("0.000000001")


@dataclass(frozen=True)
class ExitFill:
    """The booked protective exit, with the order that produced it."""

    symbol: str
    side: Side
    qty: Decimal
    price: Decimal
    filled_at: datetime
    order_price: Decimal | None
    broker: str
    reason: str
    strategy_id: str


@dataclass(frozen=True)
class EntryFill:
    """The exposure the exit closed: its fill and the protection it carried."""

    side: Side
    qty: Decimal
    price: Decimal
    filled_at: datetime
    stop_loss: Decimal
    take_profit: Decimal | None


def _num(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def _table(rows: Sequence[tuple[str, str]], left: str, right: str) -> str:
    width_left = max(len(left), *(len(key) for key, _ in rows))
    width_right = max(len(right), *(len(value) for _, value in rows))
    rule = f"+-{'-' * width_left}-+-{'-' * width_right}-+"
    lines = [rule, f"| {left:<{width_left}} | {right:<{width_right}} |", rule]
    lines.extend(f"| {key:<{width_left}} | {value:<{width_right}} |" for key, value in rows)
    lines.append(rule)
    return "\n".join(lines)


class Report:
    """Collects PASS/FAIL lines and remembers whether anything failed."""

    def __init__(self) -> None:
        self.failed = 0

    def check(self, passed: bool, label: str, detail: str) -> bool:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}: {detail}")
        if not passed:
            self.failed += 1
        return passed


def _exit_fill(session: Session, symbol: str | None, index: int) -> ExitFill | None:
    statement = (
        select(TradeRecord, OrderRecord)
        .join(OrderRecord, TradeRecord.order_id == OrderRecord.order_id)
        .where(
            OrderRecord.strategy_id == EXIT_STRATEGY,
            OrderRecord.status == OrderStatus.FILLED.value,
        )
        .order_by(TradeRecord.filled_at.desc())
        .limit(25)
    )
    if symbol is not None:
        statement = statement.where(TradeRecord.symbol == symbol)
    rows = session.execute(statement).all()
    if len(rows) < index:
        return None
    trade, order = rows[index - 1]
    return ExitFill(
        symbol=trade.symbol,
        side=Side(trade.side),
        qty=trade.qty,
        price=trade.price,
        filled_at=trade.filled_at,
        order_price=order.price,
        broker=order.broker,
        reason=order.reason,
        strategy_id=order.strategy_id,
    )


def _entry_fill(session: Session, exit_fill: ExitFill) -> EntryFill | None:
    """The most recent filled entry on the other side, before the exit."""
    statement = (
        select(OrderRecord)
        .where(
            OrderRecord.symbol == exit_fill.symbol,
            OrderRecord.strategy_id != EXIT_STRATEGY,
            OrderRecord.side == exit_fill.side.opposite.value,
            OrderRecord.status == OrderStatus.FILLED.value,
            OrderRecord.avg_fill_price.is_not(None),
            OrderRecord.approved_at <= exit_fill.filled_at,
        )
        .order_by(OrderRecord.approved_at.desc())
        .limit(1)
    )
    order = session.execute(statement).scalars().first()
    if order is None or order.avg_fill_price is None:
        return None
    return EntryFill(
        side=Side(order.side),
        qty=order.filled_qty,
        price=order.avg_fill_price,
        filled_at=order.approved_at,
        stop_loss=order.stop_loss,
        take_profit=order.take_profit,
    )


def _exit_candle(
    session: Session,
    symbol: str,
    environment: str,
    venue: str,
    timeframe: Timeframe,
    moment: datetime,
) -> CandleRow | None:
    """The newest stored bar that had closed when the exit was decided."""
    statement = (
        select(CandleRow)
        .where(
            CandleRow.environment == environment,
            CandleRow.venue == venue,
            CandleRow.symbol == symbol,
            CandleRow.timeframe == timeframe.value,
            CandleRow.close_time <= moment,
        )
        .order_by(CandleRow.close_time.desc())
        .limit(1)
    )
    return session.execute(statement).scalars().first()


def _trigger(reason: str) -> str | None:
    lowered = reason.lower()
    if "stop_loss" in lowered:
        return "stop_loss"
    if "take_profit" in lowered:
        return "take_profit"
    return None


def _infer_trigger(entry: EntryFill, bar: CandleRow) -> str | None:
    """Fallback when the reason string is unreadable: which level did the bar cross?"""
    if entry.side is Side.BUY:
        if bar.low <= entry.stop_loss:
            return "stop_loss"
        if entry.take_profit is not None and bar.high >= entry.take_profit:
            return "take_profit"
        return None
    if bar.high >= entry.stop_loss:
        return "stop_loss"
    if entry.take_profit is not None and bar.low <= entry.take_profit:
        return "take_profit"
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="audit one closed position against the candle its exit used"
    )
    parser.add_argument("--symbol", default=None, help="limit to one symbol")
    parser.add_argument("--index", type=int, default=1, help="1 = newest protective exit")
    parser.add_argument("--environment", default="testnet", help="deployment tag (testnet|mainnet)")
    parser.add_argument(
        "--venue",
        choices=["spot", "futures"],
        default=None,
        help="market of the stored bars (default: execution.market_type)",
    )
    parser.add_argument(
        "--timeframe",
        default=None,
        choices=[timeframe.value for timeframe in Timeframe],
        help="bar length the monitor reads (default: market_data.timeframes[0])",
    )
    parser.add_argument(
        "--slippage-bps",
        type=Decimal,
        default=None,
        help="adverse slippage the paper broker applies (default: execution.slippage_bps)",
    )
    args = parser.parse_args(argv)

    if args.index < 1:
        parser.error("--index must be >= 1")

    settings = load_settings()
    venue = args.venue or settings.execution.market_type.value
    timeframe = Timeframe(args.timeframe or settings.market_data.timeframes[0].value)
    slippage = (
        args.slippage_bps if args.slippage_bps is not None else settings.execution.slippage_bps
    )

    engine = create_engine_from_settings(settings.database)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            exit_fill = _exit_fill(session, args.symbol, args.index)
            if exit_fill is None:
                print(
                    f"nothing to audit: no filled {EXIT_STRATEGY} exit "
                    f"{'for ' + args.symbol if args.symbol else ''}"
                    f" (index {args.index}) in the database"
                )
                return 2
            entry_fill = _entry_fill(session, exit_fill)
            if entry_fill is None:
                print(
                    f"nothing to audit: {exit_fill.symbol} has a protective exit but no "
                    "filled entry on the other side"
                )
                return 2
            bar = _exit_candle(
                session, exit_fill.symbol, args.environment, venue, timeframe, exit_fill.filled_at
            )
            if bar is None:
                print(
                    f"nothing to audit: no stored {timeframe.value} candle for "
                    f"{exit_fill.symbol} [{args.environment}/{venue}] that closed before "
                    f"{_stamp(exit_fill.filled_at)}; run the collector or the backfill first"
                )
                return 2
    finally:
        engine.dispose()

    trigger = _trigger(exit_fill.reason) or _infer_trigger(entry_fill, bar)
    level = (
        entry_fill.stop_loss
        if trigger == "stop_loss"
        else entry_fill.take_profit
        if trigger == "take_profit"
        else entry_fill.stop_loss
    )
    is_long = entry_fill.side is Side.BUY
    closes_long = exit_fill.side is Side.SELL

    print(f"position      : {exit_fill.symbol} {'LONG' if is_long else 'SHORT'} leg")
    print(f"data source   : market.candles [{args.environment}/{venue}] {timeframe.value}")
    print(f"exit order    : {exit_fill.strategy_id}, broker {exit_fill.broker}")
    print()
    print(
        _table(
            [
                ("Entry side", entry_fill.side.value),
                ("Entry fill time", _stamp(entry_fill.filled_at)),
                ("Entry fill price", _num(entry_fill.price)),
                ("Entry qty", _num(entry_fill.qty)),
                ("Stop loss", _num(entry_fill.stop_loss)),
                (
                    "Take profit",
                    "none" if entry_fill.take_profit is None else _num(entry_fill.take_profit),
                ),
                ("Exit trigger", trigger or "unknown"),
                ("Exit level", "unknown" if level is None else _num(level)),
                ("Exit side", exit_fill.side.value),
                ("Exit fill time", _stamp(exit_fill.filled_at)),
                ("Exit fill price", _num(exit_fill.price)),
                ("Exit qty", _num(exit_fill.qty)),
                (
                    "Monitor reference price",
                    "n/a" if exit_fill.order_price is None else _num(exit_fill.order_price),
                ),
                ("Exit reason", exit_fill.reason),
            ],
            left="Item",
            right="Value",
        )
    )
    print()
    print(
        _table(
            [
                ("Exit bar open (UTC)", _stamp(bar.open_time)),
                ("Exit bar close (UTC)", _stamp(bar.close_time)),
                ("Exit bar open", _num(bar.open)),
                ("Exit bar high", _num(bar.high)),
                ("Exit bar low", _num(bar.low)),
                ("Exit bar close", _num(bar.close)),
                (
                    "Level vs bar range",
                    "unknown"
                    if level is None
                    else f"{_num(level)} in [{_num(bar.low)}, {_num(bar.high)}]",
                ),
            ],
            left="Candle",
            right="Value",
        )
    )
    print()

    report = Report()

    report.check(
        exit_fill.side is entry_fill.side.opposite,
        "the exit closes the entry leg",
        f"entry {entry_fill.side.value} -> exit {exit_fill.side.value}",
    )
    report.check(
        abs(exit_fill.qty - entry_fill.qty) <= _QTY_EPSILON,
        "the exit closes the whole entry quantity",
        f"entry {_num(entry_fill.qty)} -> exit {_num(exit_fill.qty)}",
    )
    report.check(level is not None, "the trigger level is known", str(trigger or "unknown"))

    if level is not None:
        if trigger == "stop_loss":
            breached = bar.close <= level if is_long else bar.close >= level
            touched = bar.low <= level if is_long else bar.high >= level
            rule = "close <= stop" if is_long else "close >= stop"
        else:
            breached = bar.close >= level if is_long else bar.close <= level
            touched = bar.high >= level if is_long else bar.low <= level
            rule = "close >= target" if is_long else "close <= target"
        report.check(
            breached,
            f"the exit bar's close breached the {trigger or 'level'} ({rule})",
            f"close {_num(bar.close)} vs level {_num(level)}",
        )
        report.check(
            touched,
            "the bar's range actually reached the level",
            f"{_num(bar.low)} <= {_num(level)} <= {_num(bar.high)}",
        )
    else:
        report.check(False, "the exit has no identifiable level", "cannot verify the trigger")

    if closes_long:
        inside = exit_fill.price <= bar.high
        detail = (
            f"fill {_num(exit_fill.price)} <= bar high {_num(bar.high)} (a sell never gets more)"
        )
    else:
        inside = exit_fill.price >= bar.low
        detail = f"fill {_num(exit_fill.price)} >= bar low {_num(bar.low)} (a buy never pays less)"
    report.check(inside, "the fill is not better than the bar's best price", detail)

    report.check(
        exit_fill.filled_at >= bar.close_time,
        "the exit was decided on a closed bar",
        f"filled {_stamp(exit_fill.filled_at)} >= bar close {_stamp(bar.close_time)}",
    )

    if exit_fill.broker == "paper" and exit_fill.order_price is not None:
        direction = -1 if closes_long else 1
        expected = exit_fill.order_price * (Decimal("1") + slippage / _BPS * Decimal(direction))
        report.check(
            exit_fill.price == expected,
            "the paper fill is the reference price plus adverse slippage",
            f"{_num(exit_fill.order_price)} x (1 {direction:+d} x {slippage}/{_num(_BPS)})"
            f" = {_num(expected)}, filled {_num(exit_fill.price)}",
        )

    print()
    if report.failed == 0:
        print(
            "VERDICT: protection honest -- the level was really touched, the exit was "
            "decided on a closed bar and the fill is inside that bar's range."
        )
        return 0
    print(
        f"VERDICT: PROTECTION PROBLEM -- {report.failed} check(s) failed. Investigate the "
        "position monitor and the broker before trusting this exit."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
