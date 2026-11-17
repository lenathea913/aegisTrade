"""Domain -> row mapping.

Pure functions, so the SQL shape can be inspected without a database and the
gateway/ledger never need to know about SQLAlchemy columns.
"""

from __future__ import annotations

from typing import Any, Protocol

from aegis_core import ApprovedOrder, Order, Position, Side, Trade
from aegis_ledger.audit import AuditEvent

__all__ = [
    "PositionRow",
    "TradeRow",
    "audit_row",
    "order_row",
    "position_from_row",
    "position_row",
    "trade_from_row",
    "trade_row",
]


class TradeRow(Protocol):
    """The ``trading.trades`` columns this layer reads back."""

    trade_id: str
    order_id: str
    symbol: str
    side: str
    qty: Any
    price: Any
    fee: Any
    funding: Any
    filled_at: Any
    strategy_id: str
    r_multiple: Any
    leverage: int


class PositionRow(Protocol):
    """The ``trading.positions`` columns this layer reads back."""

    symbol: str
    qty: Any
    avg_entry: Any
    unrealized_pnl: Any
    realized_pnl: Any
    opened_at: Any
    leverage: int
    stop_loss: Any
    take_profit: Any


def order_row(order: Order, approved: ApprovedOrder) -> dict[str, Any]:
    """Row for ``trading.orders``: lifecycle plus the approval evidence."""
    return {
        "order_id": order.order_id,
        "approval_id": order.approval_id,
        "symbol": order.symbol,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "qty": order.qty,
        "price": order.price,
        "stop_loss": approved.stop_loss,
        "take_profit": approved.take_profit,
        "status": order.status.value,
        "broker": order.broker,
        "exchange_order_id": order.exchange_order_id,
        "filled_qty": order.filled_qty,
        "avg_fill_price": order.avg_fill_price,
        "strategy_id": approved.strategy_id,
        "reason": approved.intent.reason,
        "max_risk_amount": approved.max_risk_amount,
        "risk_checks": list(approved.risk_checks_passed),
        "approved_at": approved.approved_at,
    }


def trade_row(trade: Trade) -> dict[str, Any]:
    """Row for ``trading.trades`` — one executed fill."""
    return {
        "trade_id": trade.trade_id,
        "order_id": trade.order_id,
        "symbol": trade.symbol,
        "side": trade.side.value,
        "qty": trade.qty,
        "price": trade.price,
        "fee": trade.fee,
        "funding": trade.funding,
        "filled_at": trade.filled_at,
        "strategy_id": trade.strategy_id,
        "r_multiple": trade.r_multiple,
        "leverage": trade.leverage,
    }


def position_row(position: Position) -> dict[str, Any]:
    """Row for ``trading.positions`` — the current state of one symbol."""
    return {
        "symbol": position.symbol,
        "qty": position.qty,
        "avg_entry": position.avg_entry,
        "unrealized_pnl": position.unrealized_pnl,
        "realized_pnl": position.realized_pnl,
        "opened_at": position.opened_at,
        "leverage": position.leverage,
        # Stored with the position, not in a side table: a restart must find the stop
        # exactly where the exposure is, and a flat row can carry neither.
        "stop_loss": position.stop_loss,
        "take_profit": position.take_profit,
    }


def trade_from_row(row: TradeRow) -> Trade:
    """Map a ``trading.trades`` row back into the domain."""
    return Trade(
        trade_id=row.trade_id,
        order_id=row.order_id,
        symbol=row.symbol,
        side=Side(row.side),
        qty=row.qty,
        price=row.price,
        fee=row.fee,
        funding=row.funding,
        filled_at=row.filled_at,
        strategy_id=row.strategy_id,
        r_multiple=row.r_multiple,
        # Rows written before futures mode have no leverage column value: they are spot.
        leverage=getattr(row, "leverage", 1),
    )


def position_from_row(row: PositionRow) -> Position:
    """Map a ``trading.positions`` row back into the domain."""
    return Position(
        symbol=row.symbol,
        qty=row.qty,
        avg_entry=row.avg_entry,
        unrealized_pnl=row.unrealized_pnl,
        realized_pnl=row.realized_pnl,
        opened_at=row.opened_at,
        leverage=getattr(row, "leverage", 1),
        stop_loss=getattr(row, "stop_loss", None),
        take_profit=getattr(row, "take_profit", None),
    )


def audit_row(event: AuditEvent) -> dict[str, Any]:
    """Row for ``audit.audit_logs``."""
    return {
        "event_type": event.event_type.value,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "actor": event.actor,
        "payload": event.payload,
        "created_at": event.created_at,
    }
