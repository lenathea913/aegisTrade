"""Reconciliation: late fills, partial deltas, cancels, and an unreachable exchange."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from aegis_core import ApprovedOrder, Order, OrderStatus, Position, Side, Trade
from aegis_execution import (
    ExchangeAuthError,
    ExchangeOrderState,
    ExchangeUnavailableError,
)
from aegis_ledger import AuditEvent, EventType, PortfolioLedger
from order_reconciler import OpenOrder, OrderReconciler

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class FakeBook:
    def __init__(self, orders: Sequence[OpenOrder]) -> None:
        self.orders = list(orders)
        self.filled: list[tuple[str, Decimal, Decimal]] = []
        self.closed: list[tuple[str, OrderStatus]] = []

    def open_orders(self) -> Sequence[OpenOrder]:
        return tuple(self.orders)

    def mark_filled(self, order: OpenOrder, *, filled_qty: Decimal, avg_price: Decimal) -> None:
        self.filled.append((order.order_id, filled_qty, avg_price))

    def mark_closed(self, order: OpenOrder, status: OrderStatus) -> None:
        self.closed.append((order.order_id, status))


class FakeExchange:
    def __init__(self, states: dict[str, ExchangeOrderState | Exception]) -> None:
        self._states = states

    def fetch(self, exchange_order_id: str, *, symbol: str) -> ExchangeOrderState:
        state = self._states[exchange_order_id]
        if isinstance(state, Exception):
            raise state
        return state


class FakeWriter:
    def __init__(self) -> None:
        self.trades: list[Trade] = []
        self.audits: list[AuditEvent] = []

    def save_trade(self, trade: Trade) -> None:
        self.trades.append(trade)

    def save_order(self, order: Order, approved: ApprovedOrder) -> None:
        return None

    def save_position(self, position: Position) -> None:
        return None

    def append_audit(self, event: AuditEvent) -> None:
        self.audits.append(event)


def open_order(**overrides: object) -> OpenOrder:
    values: dict[str, object] = {
        "order_id": "ord-1",
        "exchange_order_id": "ex-1",
        "symbol": "BTC/USDT",
        "side": Side.BUY,
        "qty": Decimal("0.1"),
        "filled_qty": Decimal("0"),
        "status": OrderStatus.SUBMITTED,
        "strategy_id": "simple_ema_cross",
    }
    values.update(overrides)
    return OpenOrder(**values)  # type: ignore[arg-type]


def reconciler(
    book: FakeBook, exchange: FakeExchange, writer: FakeWriter | None = None
) -> OrderReconciler:
    return OrderReconciler(
        ledger=PortfolioLedger(initial_equity=Decimal("10000")),
        orders=book,
        exchange=exchange,
        writer=writer,
        clock=lambda: NOW,
    )


def test_a_late_full_fill_becomes_exactly_one_trade() -> None:
    writer = FakeWriter()
    book = FakeBook([open_order()])
    exchange = FakeExchange(
        {"ex-1": ExchangeOrderState("ex-1", OrderStatus.FILLED, Decimal("0.1"), Decimal("30000"))}
    )

    results = reconciler(book, exchange, writer).run()

    assert len(results) == 1
    assert results[0].action == "filled"
    assert results[0].filled_delta == Decimal("0.1")
    assert results[0].trade is not None and results[0].trade.price == Decimal("30000")
    assert [trade.qty for trade in writer.trades] == [Decimal("0.1")]
    assert book.filled == [("ord-1", Decimal("0.1"), Decimal("30000"))]
    assert book.closed == [("ord-1", OrderStatus.FILLED)]
    assert [event.event_type for event in writer.audits] == [EventType.TRADE_RECORDED]
    assert writer.audits[0].actor == "order_reconciler"


def test_only_the_new_delta_is_applied() -> None:
    writer = FakeWriter()
    book = FakeBook([open_order(filled_qty=Decimal("0.04"))])
    exchange = FakeExchange(
        {"ex-1": ExchangeOrderState("ex-1", OrderStatus.FILLED, Decimal("0.1"), Decimal("30000"))}
    )

    results = reconciler(book, exchange, writer).run()

    assert results[0].filled_delta == Decimal("0.06")
    assert writer.trades[0].qty == Decimal("0.06")


def test_a_partial_fill_keeps_the_order_open() -> None:
    book = FakeBook([open_order()])
    exchange = FakeExchange(
        {
            "ex-1": ExchangeOrderState(
                "ex-1", OrderStatus.PARTIALLY_FILLED, Decimal("0.05"), Decimal("30000")
            )
        }
    )

    results = reconciler(book, exchange).run()

    assert results[0].action == "partially_filled"
    assert book.closed == []


def test_a_cancel_is_recorded_without_a_trade() -> None:
    writer = FakeWriter()
    book = FakeBook([open_order()])
    exchange = FakeExchange(
        {"ex-1": ExchangeOrderState("ex-1", OrderStatus.CANCELED, Decimal("0"), None)}
    )

    results = reconciler(book, exchange, writer).run()

    assert results[0].action == "closed"
    assert results[0].trade is None
    assert writer.trades == []
    assert book.closed == [("ord-1", OrderStatus.CANCELED)]
    assert [event.event_type for event in writer.audits] == [EventType.ORDER_NOT_FILLED]
    assert writer.audits[0].payload["reason"] == "exchange_canceled"


def test_an_unreachable_exchange_leaves_every_order_open() -> None:
    book = FakeBook([open_order()])
    exchange = FakeExchange({"ex-1": ExchangeUnavailableError("timed out")})

    results = reconciler(book, exchange).run()

    assert results == ()
    assert book.filled == [] and book.closed == []


def test_one_failing_order_does_not_block_the_next() -> None:
    book = FakeBook([open_order(), open_order(order_id="ord-2", exchange_order_id="ex-2")])
    exchange = FakeExchange(
        {
            "ex-1": ExchangeAuthError("401"),
            "ex-2": ExchangeOrderState(
                "ex-2", OrderStatus.FILLED, Decimal("0.1"), Decimal("30000")
            ),
        }
    )

    results = reconciler(book, exchange).run()

    assert len(results) == 1
    assert results[0].order_id == "ord-2"


def test_an_unchanged_order_is_a_no_op() -> None:
    book = FakeBook([open_order()])
    exchange = FakeExchange(
        {"ex-1": ExchangeOrderState("ex-1", OrderStatus.SUBMITTED, Decimal("0"), None)}
    )

    results = reconciler(book, exchange).run()

    assert results[0].action == "unchanged"
    assert book.filled == [] and book.closed == []
