"""Session plumbing: engine construction and factory defaults (no connection)."""

from __future__ import annotations

from sqlalchemy.pool import QueuePool

from aegis_config import DatabaseSettings
from aegis_db.session import create_engine_from_settings, create_session_factory


def test_engine_is_built_from_settings_with_pool_pre_ping() -> None:
    settings = DatabaseSettings(
        url="postgresql+psycopg://aegis:aegis@localhost:5432/aegis",
        pool_size=3,
        max_overflow=2,
        pool_timeout_seconds=7,
    )
    engine = create_engine_from_settings(settings)
    try:
        pool = engine.pool
        assert isinstance(pool, QueuePool)
        assert pool.size() == 3
        assert engine.url.database == "aegis"
    finally:
        engine.dispose()


def test_session_factory_does_not_autoflush_or_expire() -> None:
    settings = DatabaseSettings(url="postgresql+psycopg://aegis:aegis@localhost:5432/aegis")
    engine = create_engine_from_settings(settings)
    try:
        factory = create_session_factory(engine)
        assert factory.kw["autoflush"] is False
        assert factory.kw["expire_on_commit"] is False
    finally:
        engine.dispose()


def test_connect_timeout_reaches_the_driver(monkeypatch: object) -> None:
    """Without it a stopped PostgreSQL makes health checks hang instead of failing."""
    from typing import Any

    from aegis_db import session as session_module

    captured: dict[str, Any] = {}

    def fake_create_engine(url: str, **kwargs: Any) -> Any:
        captured["url"] = url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(session_module, "create_engine", fake_create_engine)  # type: ignore[attr-defined]
    settings = DatabaseSettings(
        url="postgresql+psycopg://aegis:aegis@localhost:5432/aegis",
        connect_timeout_seconds=3,
    )

    session_module.create_engine_from_settings(settings)

    assert captured["connect_args"] == {"connect_timeout": 3}
    assert captured["pool_pre_ping"] is True
