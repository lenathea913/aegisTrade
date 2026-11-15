"""The portfolio ledger: the single source of truth for exposure and equity.

Accounting model (deliberately simple and reconcilable):

    equity = initial_equity + realized_pnl + unrealized_pnl(mark)

* ``realized_pnl`` accumulates every closed quantity (from ``Position.apply_fill``)
  **minus fees and plus funding**; each change is kept as a timestamped
  ``RealizedEvent`` so daily/weekly loss windows can be recomputed exactly instead
  of being a mutable running counter.
* ``unrealized_pnl`` is derived from the last mark, never stored as truth.
* Quantities are signed (long positive, short negative) and carried by the domain
  ``Position``, so flipping through zero is one tested code path.

The ledger holds no database handle: it is pure, deterministic and testable.
``aegis_ledger.store.LedgerStore`` persists what it computes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from aegis_core import PortfolioState, Position, Side, Trade, utc_now
from aegis_ledger.errors import LedgerError

__all__ = [
    "LedgerUpdate",
    "PortfolioLedger",
    "PositionProtection",
    "RealizedEvent",
    "ReconstructionSession",
    "ReconstructionSummary",
]

_ZERO = Decimal("0")


class ReconstructionSession(Protocol):
    """The slice of ``Session`` reconstruction needs.

    Narrowing it to ``execute`` lets the replay be tested without a database while
    a real ``Session`` still satisfies it structurally.
    """

    def execute(self, statement: Any) -> Any: ...


@dataclass(frozen=True)
class PositionProtection:
    """The exit levels attached to an open position.

    ``PortfolioLedger`` keeps these beside the position so a monitor can answer
    "where is my stop?" without re-reading the order table, and so the levels move
    with the state that produced them (a flat position has no protection).
    """

    symbol: str
    side: Side
    qty: Decimal
    stop_loss: Decimal
    take_profit: Decimal | None = None
    strategy_id: str | None = None

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise LedgerError("protection qty must be greater than 0")
        if self.stop_loss <= 0:
            raise LedgerError("protection stop_loss must be greater than 0")
        if self.take_profit is not None and self.take_profit <= 0:
            raise LedgerError("protection take_profit must be greater than 0")
        if (
            self.side is Side.BUY
            and self.take_profit is not None
            and self.stop_loss >= self.take_profit
        ):
            raise LedgerError("long protection needs stop_loss < take_profit")
        if (
            self.side is Side.SELL
            and self.take_profit is not None
            and self.stop_loss <= self.take_profit
        ):
            raise LedgerError("short protection needs stop_loss > take_profit")


@dataclass(frozen=True)
class RealizedEvent:
    """One change in realized PnL, with the time it happened."""

    amount: Decimal
    at: datetime
    trade_id: str
    symbol: str


@dataclass(frozen=True)
class LedgerUpdate:
    """What one applied trade changed."""

    trade: Trade
    position: Position
    realized_delta: Decimal
    equity: Decimal


@dataclass(frozen=True)
class ReconstructionSummary:
    """What a startup reconstruction found in the durable record."""

    trades_replayed: int
    positions_loaded: int
    open_positions: int
    daily_realized_pnl: Decimal
    weekly_realized_pnl: Decimal
    equity: Decimal
    replayed_at: datetime


@dataclass
class PortfolioLedger:
    """Signed positions, realized PnL history, marks and equity."""

    initial_equity: Decimal
    _positions: dict[str, Position] = field(default_factory=dict, repr=False)
    _marks: dict[str, Decimal] = field(default_factory=dict, repr=False)
    _realized: list[RealizedEvent] = field(default_factory=list, repr=False)
    _protections: dict[str, PositionProtection] = field(default_factory=dict, repr=False)
    _clock: Callable[[], datetime] = field(default=utc_now, repr=False)

    def __post_init__(self) -> None:
        if self.initial_equity <= 0:
            raise LedgerError("initial_equity must be greater than 0")

    # ------------------------------------------------------------- reads

    @property
    def positions(self) -> tuple[Position, ...]:
        return tuple(self._positions.values())

    @property
    def realized_events(self) -> tuple[RealizedEvent, ...]:
        return tuple(self._realized)

    @property
    def realized_pnl(self) -> Decimal:
        return sum((event.amount for event in self._realized), _ZERO)

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum((position.unrealized_pnl for position in self._marked_positions()), _ZERO)

    @property
    def equity(self) -> Decimal:
        return self.initial_equity + self.realized_pnl + self.unrealized_pnl

    def position(self, symbol: str) -> Position:
        """Current position for ``symbol`` (flat when none is held)."""
        wanted = Position.flat(symbol).symbol
        return self._positions.get(wanted, Position.flat(wanted))

    def mark(self, symbol: str) -> Decimal | None:
        wanted = Position.flat(symbol).symbol
        return self._marks.get(wanted)

    def protection(self, symbol: str) -> PositionProtection | None:
        """Exit levels for ``symbol``, or ``None`` when the position is unprotected."""
        return self._protections.get(Position.flat(symbol).symbol)

    @property
    def protections(self) -> tuple[PositionProtection, ...]:
        return tuple(self._protections.values())

    def realized_between(self, start: datetime, end: datetime | None = None) -> Decimal:
        """Realized PnL for events in ``[start, end]`` (``end`` defaults to now)."""
        upper = end if end is not None else self._clock()
        return sum(
            (event.amount for event in self._realized if start <= event.at <= upper),
            _ZERO,
        )

    # ------------------------------------------------------------ writes

    def apply_trade(self, trade: Trade) -> LedgerUpdate:
        """Apply one executed trade: update the position, costs and marks."""
        current = self.position(trade.symbol)
        updated = current.apply_fill(trade.signed_qty, trade.price)

        # Costs belong to the realization moment, so the equity effect is immediate.
        realized_delta = (updated.realized_pnl - current.realized_pnl) - trade.fee + trade.funding
        self._positions[updated.symbol] = updated
        self._marks[updated.symbol] = trade.price
        if updated.is_flat:
            # A flat position cannot have a stop; leaving one behind would let a
            # monitor fire an exit against exposure that no longer exists.
            self._protections.pop(updated.symbol, None)
        self._realized.append(
            RealizedEvent(
                amount=realized_delta,
                at=trade.filled_at,
                trade_id=trade.trade_id,
                symbol=updated.symbol,
            )
        )

        return LedgerUpdate(
            trade=trade,
            position=updated,
            realized_delta=realized_delta,
            equity=self.equity,
        )

    def mark_price(self, symbol: str, price: Decimal) -> None:
        """Mark ``symbol`` to ``price``; the position's unrealized PnL follows."""
        if price <= 0:
            raise LedgerError("mark price must be greater than 0")
        current = self.position(symbol)
        self._positions[current.symbol] = current.with_mark(price)
        self._marks[current.symbol] = price

    def mark_prices(self, prices: Mapping[str, Decimal]) -> None:
        for symbol, price in prices.items():
            self.mark_price(symbol, price)

    def set_protection(self, protection: PositionProtection) -> None:
        """Attach exit levels to an open position (replacing any previous ones)."""
        wanted = Position.flat(protection.symbol).symbol
        if self.position(wanted).is_flat:
            raise LedgerError(f"cannot protect a flat position: {wanted}")
        self._protections[wanted] = protection

    def clear_protection(self, symbol: str) -> None:
        self._protections.pop(Position.flat(symbol).symbol, None)

    # ------------------------------------------------------------ snapshots

    def snapshot(self, *, now: datetime | None = None) -> PortfolioState:
        """Frozen view handed to the risk engine (never a live query)."""
        moment = now if now is not None else self._clock()
        day_start = datetime(moment.year, moment.month, moment.day, tzinfo=UTC)
        week_start = day_start - timedelta(days=moment.weekday())

        if self.equity <= 0:
            raise LedgerError(f"equity depleted ({self.equity}); trading must stop")

        return PortfolioState(
            equity=self.equity,
            daily_realized_pnl=self.realized_between(day_start, moment),
            weekly_realized_pnl=self.realized_between(week_start, moment),
            positions=self._marked_positions(),
        )

    def reconstruct_from_db(
        self,
        session: ReconstructionSession,
        *,
        strict: bool = True,
        now: datetime | None = None,
    ) -> ReconstructionSummary:
        """Rebuild positions, marks and realized PnL from ``trading.trades``.

        Trades are the immutable record, so the ledger replays them **in order** with
        the same accounting it uses live (``Position.apply_fill``, fees and funding
        booked at the realization moment). That yields the day/week windows and the
        final positions without trusting a mutable row.

        ``trading.positions`` is then compared against the replay. With ``strict`` a
        disagreement raises ``LedgerError``: the tables are the durable record, and a
        silent divergence would make `/positions` a nice-looking lie.
        """
        from sqlalchemy import select

        from aegis_db import PositionRecord, TradeRecord
        from aegis_ledger.records import position_from_row, trade_from_row

        replayed = [
            trade_from_row(row)
            for row in session.execute(
                select(TradeRecord).order_by(TradeRecord.filled_at, TradeRecord.trade_id)
            ).scalars()
        ]
        stored = [
            position_from_row(row) for row in session.execute(select(PositionRecord)).scalars()
        ]

        self._positions.clear()
        self._marks.clear()
        self._realized.clear()
        self._protections.clear()

        for trade in replayed:
            current = self.position(trade.symbol)
            updated = current.apply_fill(trade.signed_qty, trade.price)
            realized_delta = (
                (updated.realized_pnl - current.realized_pnl) - trade.fee + trade.funding
            )
            self._positions[updated.symbol] = updated
            self._marks[updated.symbol] = trade.price
            self._realized.append(
                RealizedEvent(
                    amount=realized_delta,
                    at=trade.filled_at,
                    trade_id=trade.trade_id,
                    symbol=updated.symbol,
                )
            )

        mismatches = self._compare_positions(stored)
        if mismatches and strict:
            raise LedgerError(
                "reconstructed ledger disagrees with trading.positions: " + "; ".join(mismatches)
            )

        moment = now if now is not None else self._clock()
        state = self.snapshot(now=moment)
        return ReconstructionSummary(
            trades_replayed=len(replayed),
            positions_loaded=len(stored),
            open_positions=len(state.open_positions),
            daily_realized_pnl=state.daily_realized_pnl,
            weekly_realized_pnl=state.weekly_realized_pnl,
            equity=state.equity,
            replayed_at=moment,
        )

    def _compare_positions(self, stored: Sequence[Position]) -> list[str]:
        """Differences between the replay and the positions table (empty = consistent)."""
        differences: list[str] = []
        by_symbol = {position.symbol: position for position in stored}
        for symbol in sorted(set(by_symbol) | set(self._positions)):
            row = by_symbol.get(symbol)
            replayed = self._positions.get(symbol)
            if row is None:
                differences.append(f"{symbol}: missing position row")
                continue
            if replayed is None:
                if not row.is_flat:
                    differences.append(f"{symbol}: row holds {row.qty} with no trades")
                continue
            if row.qty != replayed.qty:
                differences.append(f"{symbol}: qty row={row.qty} replay={replayed.qty}")
            if row.realized_pnl != replayed.realized_pnl:
                differences.append(
                    f"{symbol}: realized row={row.realized_pnl} replay={replayed.realized_pnl}"
                )
        return differences

    def restore(
        self, positions: Sequence[Position], realized: Sequence[RealizedEvent] = ()
    ) -> None:
        """Reload state (e.g. from the database) without replaying every trade."""
        for position in positions:
            self._positions[position.symbol] = position
        self._realized[:] = list(realized)
        self._protections.clear()

    def _marked_positions(self) -> tuple[Position, ...]:
        return tuple(
            position.with_mark(price) if (price := self._marks.get(position.symbol)) else position
            for position in self._positions.values()
        )
