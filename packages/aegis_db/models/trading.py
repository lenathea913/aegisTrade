"""``trading.*``: orders, positions, executed trades and signals.

These tables are written by the execution gateway and the portfolio ledger. Enum
columns are stored as short ``VARCHAR`` with CHECK constraints rather than native
PostgreSQL enums: the allowed sets are validated in the domain, and a CHECK is a
plain, reversible migration when the domain grows, whereas ``ALTER TYPE ... ADD
VALUE`` cannot run inside a transaction before PG12 and never inside one safely.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aegis_core.enums import OrderStatus, OrderType, Side
from aegis_core.signal import SignalDirection, SignalStatus
from aegis_db.base import Base, TimestampMixin

_SIDE_SQL = ", ".join(f"'{side.value}'" for side in Side)
_ORDER_TYPE_SQL = ", ".join(f"'{order_type.value}'" for order_type in OrderType)
_STATUS_SQL = ", ".join(f"'{status.value}'" for status in OrderStatus)
_DIRECTION_SQL = ", ".join(f"'{direction.value}'" for direction in SignalDirection)
_SIGNAL_STATUS_SQL = ", ".join(f"'{status.value}'" for status in SignalStatus)


class OrderRecord(Base, TimestampMixin):
    """The submitted order plus the approval evidence that allowed it."""

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(f"side IN ({_SIDE_SQL})", name="ck_orders_side_known"),
        CheckConstraint(f"order_type IN ({_ORDER_TYPE_SQL})", name="ck_orders_order_type_known"),
        CheckConstraint(f"status IN ({_STATUS_SQL})", name="ck_orders_status_known"),
        CheckConstraint("qty > 0", name="ck_orders_qty_positive"),
        CheckConstraint("price IS NULL OR price > 0", name="ck_orders_price_positive"),
        CheckConstraint("stop_loss > 0", name="ck_orders_stop_loss_positive"),
        CheckConstraint(
            "take_profit IS NULL OR take_profit > 0", name="ck_orders_take_profit_positive"
        ),
        CheckConstraint(
            "filled_qty >= 0 AND filled_qty <= qty", name="ck_orders_filled_within_qty"
        ),
        CheckConstraint("max_risk_amount > 0", name="ck_orders_max_risk_amount_positive"),
        Index("ix_orders_symbol_created_at", "symbol", "created_at"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_strategy_id", "strategy_id"),
        Index(
            "uq_orders_exchange_order_id",
            "exchange_order_id",
            unique=True,
            postgresql_where=text("exchange_order_id IS NOT NULL"),
        ),
        {"schema": "trading"},
    )

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    approval_id: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(8), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    stop_loss: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    broker: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    filled_qty: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, server_default=text("0")
    )
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)

    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    max_risk_amount: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    risk_checks: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PositionRecord(Base):
    """Current exposure per symbol. One row per symbol; the ledger owns it."""

    __tablename__ = "positions"
    __table_args__ = (
        CheckConstraint(
            "(qty <> 0 AND avg_entry > 0) OR (qty = 0 AND avg_entry = 0)",
            name="ck_positions_flat_has_no_basis",
        ),
        # The stop belongs to an open leg: a flat row must not keep one, or a restart
        # would watch an exit for exposure that no longer exists.
        CheckConstraint(
            "(qty = 0 AND stop_loss IS NULL AND take_profit IS NULL) OR "
            "(qty <> 0 AND (stop_loss IS NULL OR stop_loss > 0) "
            "AND (take_profit IS NULL OR take_profit > 0))",
            name="ck_positions_protection_matches_exposure",
        ),
        # A venue order id is only meaningful while the exposure and its level exist.
        CheckConstraint(
            "(qty = 0 AND stop_order_id IS NULL AND tp_order_id IS NULL) OR "
            "(qty <> 0 AND (stop_order_id IS NULL OR stop_loss IS NOT NULL) "
            "AND (tp_order_id IS NULL OR take_profit IS NOT NULL))",
            name="ck_positions_protection_orders_match_exposure",
        ),
        {"schema": "trading"},
    )

    symbol: Mapped[str] = mapped_column(String(24), primary_key=True)
    qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False, server_default=text("0"))
    avg_entry: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, server_default=text("0")
    )
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, server_default=text("0")
    )
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, server_default=text("0")
    )
    #: Margin multiplier of the open leg (1 = spot). Display and ROE only: PnL on a
    #: linear contract is independent of leverage.
    leverage: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    #: Protective levels of the open leg. Persisted here so a restart can re-arm the
    #: position monitor instead of leaving the position unprotected.
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    #: Venue-side order ids of those exits. Stored so a restart can ask the exchange
    #: whether the stop is still working, and re-arm it when it is not.
    stop_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tp_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ReconciliationStateRecord(Base):
    """One row: whether the ledger still agrees with the venue.

    The flag lives in the database, not in a process, because "no entries until an
    operator looks" has to survive the restart that a drift usually causes. The row is
    a singleton (``id = 1``) updated in place; history lives in the audit log.
    """

    __tablename__ = "reconciliation_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_reconciliation_state_singleton"),
        CheckConstraint(
            "status IN ('synced', 'drift', 'unchecked')",
            name="ck_reconciliation_state_status_known",
        ),
        CheckConstraint("drift_count >= 0", name="ck_reconciliation_state_count_non_negative"),
        {"schema": "trading"},
    )

    id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, server_default=text("1"), autoincrement=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'unchecked'")
    )
    #: True while entries are refused because the book and the venue disagree.
    entries_halted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    drift_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    detail: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TradeRecord(Base):
    """One executed trade. ``order_id`` may repeat when an order fills in parts."""

    __tablename__ = "trades"
    __table_args__ = (
        CheckConstraint(f"side IN ({_SIDE_SQL})", name="ck_trades_side_known"),
        CheckConstraint("qty > 0", name="ck_trades_qty_positive"),
        CheckConstraint("price > 0", name="ck_trades_price_positive"),
        CheckConstraint("fee >= 0", name="ck_trades_fee_non_negative"),
        Index("ix_trades_order_id", "order_id"),
        Index("ix_trades_strategy_id_filled_at", "strategy_id", "filled_at"),
        Index("ix_trades_symbol_filled_at", "symbol", "filled_at"),
        {"schema": "trading"},
    )

    trade_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("trading.orders.order_id", ondelete="RESTRICT"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    fee: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False, server_default=text("0"))
    funding: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, server_default=text("0")
    )
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    r_multiple: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    #: Leverage of the leg this fill belonged to (1 = spot/unleveraged).
    leverage: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class SignalRecord(Base):
    """One trading idea, keyed by the closed bar that produced it.

    Generation is deterministic — one bar closes, one idea — so an idea is unique per
    ``(strategy_id, symbol, timeframe, bar_close_time)``. That key is what makes a
    restart idempotent: the rerun of the same bar upserts into the row that already
    exists, and a signal the operator already converted or ignored stays that way.
    """

    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint(
            "strategy_id",
            "symbol",
            "timeframe",
            "bar_close_time",
            name="uq_signals_idea_bar",
        ),
        CheckConstraint(f"direction IN ({_DIRECTION_SQL})", name="ck_signals_direction_known"),
        CheckConstraint(f"status IN ({_SIGNAL_STATUS_SQL})", name="ck_signals_status_known"),
        CheckConstraint("entry_ref > 0", name="ck_signals_entry_ref_positive"),
        CheckConstraint("stop_loss > 0", name="ck_signals_stop_loss_positive"),
        CheckConstraint("take_profit > 0", name="ck_signals_take_profit_positive"),
        CheckConstraint("risk_reward > 0", name="ck_signals_risk_reward_positive"),
        CheckConstraint("expires_at > created_at", name="ck_signals_expiry_after_creation"),
        Index("ix_signals_symbol_created_at", "symbol", "created_at"),
        Index("ix_signals_status", "status"),
        {"schema": "trading"},
    )

    signal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    entry_ref: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    stop_loss: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    take_profit: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    risk_reward: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    bar_close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason_codes: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    market_context: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    status_reasons: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
