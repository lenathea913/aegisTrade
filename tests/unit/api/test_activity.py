"""The activity feed: JSON endpoint, HTMX partial and the unconfigured case."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from aegis_config import load_settings
from aegis_core import Trade
from aegis_db import ActivityRecord
from aegis_ledger import AuditEvent, PortfolioLedger
from api.app import create_app
from api.service import ApiService

NOW = datetime(2026, 11, 20, 7, 0, tzinfo=UTC)


class EmptyRepository:
    """The activity routes never touch the trading repository."""

    def ping(self) -> bool:
        return True

    def reconstruct(self, ledger: PortfolioLedger, *, strict: bool = True) -> None:
        """Offline fake: nothing is stored, so the book keeps what the test set."""

    def trades(self, limit: int) -> Sequence[Trade]:
        return ()

    def audit_events(self, limit: int) -> Sequence[AuditEvent]:
        return ()


class FakeActivity:
    def __init__(self, records: Sequence[ActivityRecord]) -> None:
        self._records = tuple(records)
        self.calls: list[int] = []

    def recent(self, limit: int) -> Sequence[ActivityRecord]:
        self.calls.append(limit)
        return self._records[:limit]


def records() -> list[ActivityRecord]:
    return [
        ActivityRecord(
            id=3,
            at=NOW,
            event_type="RUNNER_HEARTBEAT",
            level="INFO",
            message="Evaluated BTC/USDT: no crossover signal.",
        ),
        ActivityRecord(
            id=2,
            at=NOW - timedelta(minutes=1),
            event_type="ORDER_REJECTED",
            level="WARNING",
            message="Order rejected for BTC/USDT: max_exposure_per_symbol exceeded.",
        ),
    ]


def build_client(activity: FakeActivity | None = None) -> TestClient:
    settings = load_settings("paper")
    service = ApiService(
        settings=settings,
        repository=EmptyRepository(),
        ledger=PortfolioLedger(initial_equity=settings.risk.initial_equity),
    )
    app = create_app(settings=settings, service=service, activity=activity)
    return TestClient(app)


def test_api_returns_the_newest_entries_first() -> None:
    client = build_client(FakeActivity(records()))

    response = client.get("/api/activity?limit=50")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert [entry["event_type"] for entry in payload["entries"]] == [
        "RUNNER_HEARTBEAT",
        "ORDER_REJECTED",
    ]
    assert payload["entries"][0]["message"].startswith("Evaluated BTC/USDT")
    assert payload["entries"][1]["level"] == "WARNING"


def test_limit_is_passed_through_and_bounded() -> None:
    feed = FakeActivity(records())
    client = build_client(feed)

    assert client.get("/api/activity?limit=1").json()["count"] == 1
    assert feed.calls == [1]
    assert client.get("/api/activity?limit=0").status_code == 422
    assert client.get("/api/activity?limit=201").status_code == 422


def test_partial_renders_levels_and_messages() -> None:
    client = build_client(FakeActivity(records()))

    response = client.get("/partials/activity")

    assert response.status_code == 200
    body = response.text
    assert "Evaluated BTC/USDT: no crossover signal." in body
    assert "WARNING" in body
    assert "am-amber" in body or "bg-amber-500/15" in body


def test_page_embeds_the_polling_trigger() -> None:
    client = build_client(FakeActivity(records()))

    body = client.get("/activity").text

    assert 'hx-get="/partials/activity"' in body
    assert 'hx-trigger="every 3s"' in body
    assert "Activity" in body


def test_empty_feed_renders_an_explanation() -> None:
    client = build_client(FakeActivity([]))

    body = client.get("/partials/activity").text

    assert "No activity yet" in body


def test_unconfigured_feed_is_a_503_not_an_empty_list() -> None:
    client = build_client(None)

    assert client.get("/api/activity").status_code == 503
    assert client.get("/partials/activity").status_code == 503
