"""SQLAlchemy declarative base.

Deterministic constraint names matter for migrations: Alembic cannot reliably
alter an unnamed constraint on PostgreSQL 15. Check constraints are named
explicitly at the call site (full name, no convention), because the naming
convention's ``%(constraint_name)s`` token would otherwise double the prefix.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

__all__ = ["NAMING_CONVENTION", "Base", "TimestampMixin"]


class Base(DeclarativeBase):
    """Declarative base for every AegisTrade table."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """Insert/update timestamps, stored as ``timestamptz`` in UTC."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
