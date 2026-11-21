"""Default application wiring: engine lifecycle and the ASGI entry point."""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from aegis_config import load_settings
from aegis_core import Trade
from aegis_ledger import AuditEvent, PortfolioLedger
from api import app as app_module
from api.service import DEFAULT_JOURNAL_LIMIT


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class EmptyRepository:
    def ping(self) -> bool:
        return True

    def reconstruct(self, ledger: PortfolioLedger, *, strict: bool = True) -> None:
        """Offline fake: nothing is stored, so the book keeps what the test set."""

    def trades(self, limit: int) -> Sequence[Trade]:
        return []

    def audit_events(self, limit: int) -> Sequence[AuditEvent]:
        return []


class UnreachableFactory:
    """Startup reconstruction must degrade, not crash, without a database."""

    def __call__(self) -> Any:
        raise SQLAlchemyError("database is unreachable")


def test_default_wiring_builds_a_service_and_disposes_the_engine(monkeypatch: Any) -> None:
    engine = FakeEngine()
    monkeypatch.setattr(app_module, "create_engine_from_settings", lambda database: engine)
    monkeypatch.setattr(app_module, "create_session_factory", lambda eng: UnreachableFactory())
    monkeypatch.setattr(app_module, "SqlTradingRepository", lambda factory: EmptyRepository())
    settings = load_settings("paper")

    with TestClient(app_module.create_app(settings=settings)) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/journal").json()["limit"] == DEFAULT_JOURNAL_LIMIT

    assert engine.disposed is True


def test_asgi_entry_point_exposes_an_app() -> None:
    module = importlib.import_module("api.main")

    assert module.app.title == "AegisTrade API"
