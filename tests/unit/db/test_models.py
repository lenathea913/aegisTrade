"""Schema contract tests.

No database is required: these assert the ORM metadata (names, keys, constraints,
types) and that the initial migration carries the parts Alembic cannot generate
(schemas, append-only trigger).
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import BigInteger, DateTime, Numeric, Table
from sqlalchemy.dialects.postgresql import JSONB

from aegis_core.enums import OrderStatus, Side, Timeframe
from aegis_db import AuditLog, Base, Candle, OrderRecord, PositionRecord, TradeRecord

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "data" / "migrations" / "versions" / "0001_initial_schema.py"

EXPECTED_TABLES = {
    ("market", "candles"),
    ("trading", "orders"),
    ("trading", "positions"),
    ("trading", "trades"),
    ("trading", "signals"),
    ("trading", "reconciliation_state"),
    ("trading", "bot_activity"),
    ("audit", "audit_logs"),
}


def table_of(model: type[Base]) -> Table:
    """``model.__table__`` with a precise type for mypy strict."""
    return cast(Table, model.__table__)


def test_metadata_registers_exactly_the_expected_tables() -> None:
    tables = {(table.schema, table.name) for table in Base.metadata.tables.values()}
    assert tables == EXPECTED_TABLES


def test_candles_use_the_natural_key_for_idempotent_inserts() -> None:
    table = table_of(Candle)
    assert [column.name for column in table.primary_key.columns] == [
        "environment",
        "symbol",
        "timeframe",
        "open_time",
    ]
    assert table.primary_key.name == "pk_candles"


@pytest.mark.parametrize("name", ["open", "high", "low", "close", "volume"])
def test_ohlcv_columns_are_exact_decimals(name: str) -> None:
    column_type = table_of(Candle).columns[name].type
    assert isinstance(column_type, Numeric)
    assert column_type.precision == 28
    assert column_type.scale == 12


def test_candle_timestamps_are_timezone_aware() -> None:
    for name in ("open_time", "close_time", "ingested_at"):
        column_type = table_of(Candle).columns[name].type
        assert isinstance(column_type, DateTime)
        assert column_type.timezone is True


def test_candle_checks_cover_timeframes_and_ohlc_sanity() -> None:
    checks = {
        constraint.name
        for constraint in table_of(Candle).constraints
        if isinstance(constraint.name, str) and constraint.name.startswith("ck_")
    }
    assert checks == {
        "ck_candles_environment_known",
        "ck_candles_timeframe_known",
        "ck_candles_open_before_close",
        "ck_candles_ohlc_bounds",
        "ck_candles_low_positive",
        "ck_candles_volume_non_negative",
    }


def test_every_timeframe_value_is_storable() -> None:
    check = next(
        constraint
        for constraint in table_of(Candle).constraints
        if constraint.name == "ck_candles_timeframe_known"
    )
    sql = str(getattr(check, "sqltext", ""))
    for timeframe in Timeframe:
        assert f"'{timeframe.value}'" in sql


def test_orders_keep_the_approval_evidence() -> None:
    table = table_of(OrderRecord)
    for required in ("approval_id", "max_risk_amount", "risk_checks", "approved_at"):
        assert required in table.columns
    assert table.columns["approval_id"].nullable is False
    assert table.columns["max_risk_amount"].nullable is False
    assert isinstance(table.columns["risk_checks"].type, JSONB)


def test_orders_status_and_side_checks_are_complete() -> None:
    checks = {
        constraint.name: str(getattr(constraint, "sqltext", ""))
        for constraint in table_of(OrderRecord).constraints
        if isinstance(constraint.name, str) and constraint.name.startswith("ck_")
    }
    for status in OrderStatus:
        assert status.value in checks["ck_orders_status_known"]
    for side in Side:
        assert side.value in checks["ck_orders_side_known"]
    assert "filled_qty >= 0 AND filled_qty <= qty" in checks["ck_orders_filled_within_qty"]


def test_exchange_order_id_is_uniquely_indexed_only_when_present() -> None:
    index = next(
        idx for idx in table_of(OrderRecord).indexes if idx.name == "uq_orders_exchange_order_id"
    )
    assert index.unique is True
    assert index.dialect_options["postgresql"]["where"] is not None


def test_positions_enforce_the_flat_invariant() -> None:
    checks = {constraint.name for constraint in table_of(PositionRecord).constraints}
    assert "ck_positions_flat_has_no_basis" in checks


def test_trades_reference_orders_and_refuse_deletion() -> None:
    table = table_of(TradeRecord)
    (foreign_key,) = list(table.columns["order_id"].foreign_keys)
    assert foreign_key.target_fullname == "trading.orders.order_id"
    assert foreign_key.ondelete == "RESTRICT"
    assert foreign_key.constraint is not None
    assert foreign_key.constraint.name == "fk_trades_order_id_orders"


def test_trades_carry_fee_funding_and_strategy() -> None:
    table = table_of(TradeRecord)
    for required in ("fee", "funding", "strategy_id", "r_multiple", "filled_at"):
        assert required in table.columns
    assert table.columns["fee"].nullable is False


def test_audit_logs_are_bigserial_with_a_jsonb_payload() -> None:
    table = table_of(AuditLog)
    assert isinstance(table.columns["id"].type, BigInteger)
    assert table.columns["id"].primary_key is True
    assert isinstance(table.columns["payload"].type, JSONB)
    assert table.columns["payload"].nullable is False


def test_all_timestamps_are_timezone_aware() -> None:
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, DateTime):
                assert column.type.timezone is True, f"{table.fullname}.{column.name} is naive"


@pytest.mark.parametrize(
    ("model", "schema"),
    [
        (OrderRecord, "trading"),
        (PositionRecord, "trading"),
        (TradeRecord, "trading"),
        (AuditLog, "audit"),
    ],
)
def test_tables_are_in_their_namespace(model: type[Base], schema: str) -> None:
    assert table_of(model).schema == schema


def test_initial_migration_creates_schemas_and_the_append_only_guard() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'CREATE SCHEMA IF NOT EXISTS "{schema}"' in source
    assert 'SCHEMAS: tuple[str, ...] = ("market", "trading", "audit")' in source
    assert "CREATE TRIGGER trg_audit_logs_append_only" in source
    assert "BEFORE UPDATE OR DELETE ON audit.audit_logs" in source
    assert "BEFORE TRUNCATE ON audit.audit_logs" in source


def test_migration_declares_revision_0001_without_a_parent() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0001"' in source
    assert "down_revision: str | None = None" in source
