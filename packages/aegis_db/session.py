"""Engine and session plumbing.

Sessions are short-lived and explicit: services open a ``session_scope`` for one
unit of work and commit once. ``autoflush`` is off so nothing reaches the database
before the code says so, and ``expire_on_commit`` is off so domain objects can be
read after the scope closes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from aegis_config.settings import DatabaseSettings

__all__ = ["create_engine_from_settings", "create_session_factory", "session_scope"]


def create_engine_from_settings(database: DatabaseSettings) -> Engine:
    """Build a pooled PostgreSQL engine with ``pool_pre_ping`` and a connect timeout.

    The timeout matters for health checks and the API: without it a firewalled or
    stopped PostgreSQL makes ``/health`` hang instead of reporting ``degraded``.
    """
    return create_engine(
        database.url,
        echo=database.echo,
        connect_args={"connect_timeout": database.connect_timeout_seconds},
        pool_size=database.pool_size,
        max_overflow=database.max_overflow,
        pool_timeout=database.pool_timeout_seconds,
        pool_pre_ping=True,
        future=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory bound to ``engine``."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit on success, roll back on any exception, always close."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
