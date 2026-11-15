"""Domain -> row mapping.

Pure functions, so the SQL shape can be inspected without a database and the
gateway/ledger never need to know about SQLAlchemy columns.
"""

from __future__ import annotations

from typing import Any

from aegis_core import ApprovedOrder, Order, Position, Trade
from aegis_ledger.audit import AuditEvent

__all__ = ["audit_row", "order_row", "position_row", "trade_row"]


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
    }


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
