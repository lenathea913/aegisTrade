"""Open-order visibility: broker mapping, API routes and the dashboard page."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from aegis_config import Settings, load_settings
from aegis_core import OrderStatus, Side, Trade
from aegis_execution import ExchangeError, ExchangeOrderState
from aegis_ledger import AuditEvent, PortfolioLedger
from api.app import create_app
from api.exchange import cancel_order, open_orders
from api.service import ApiService

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class FakeBroker:
    """A live broker stand-in: fixed open orders, recorded cancels."""

    live = True

    def __init__(self, *, orders: Sequence[ExchangeOrderState] = ()) -> None:
        self._orders = tuple(orders)
        self.cancelled: list[tuple[str, str]] = []
        self.closed = False

    def fetch_open_orders(self) -> tuple[ExchangeOrderState, ...]:
        return self._orders

    def cancel(self, exchange_order_id: str, *, symbol: str) -> ExchangeOrderState:
        self.cancelled.append((exchange_order_id, symbol))
        return ExchangeOrderState(exchange_order_id, OrderStatus.CANCELED, Decimal("0"), None)

    def close(self) -> None:
        self.closed = True


class PaperBrokerStub:
    live = False

    def close(self) -> None:  # pragma: no cover - not called by the API
        return None


class BrokerWithoutCancel:
    live = True

    def fetch_open_orders(self) -> tuple[ExchangeOrderState, ...]:
        return ()

    def close(self) -> None:
        return None


def settings() -> Settings:
    return load_settings("paper")


def open_order() -> ExchangeOrderState:
    return ExchangeOrderState(
        exchange_order_id="111",
        status=OrderStatus.PARTIALLY_FILLED,
        filled_qty=Decimal("0.005"),
        avg_price=Decimal("29000"),
        symbol="BTC/USDT",
        side=Side.BUY,
        price=Decimal("29000"),
        qty=Decimal("0.01"),
    )


def test_open_orders_are_mapped_and_the_broker_is_closed() -> None:
    broker = FakeBroker(orders=(open_order(),))

    views = open_orders(settings(), factory=lambda *_a, **_k: broker)

    assert broker.closed is True
    assert len(views) == 1
    assert views[0].symbol == "BTC/USDT"
    assert views[0].side == "BUY"
    assert views[0].status == "PARTIALLY_FILLED"
    assert views[0].filled_qty == Decimal("0.005")


def test_paper_mode_makes_no_venue_call() -> None:
    broker = PaperBrokerStub()

    assert open_orders(settings(), factory=lambda *_a, **_k: broker) == ()


def test_cancel_returns_the_exchange_status_and_closes_the_broker() -> None:
    broker = FakeBroker()

    status = cancel_order(settings(), "111", "BTC/USDT", factory=lambda *_a, **_k: broker)

    assert status == "CANCELED"
    assert broker.cancelled == [("111", "BTC/USDT")]
    assert broker.closed is True


def test_a_broker_without_cancel_refuses_clearly() -> None:
    import pytest

    broker = BrokerWithoutCancel()

    with pytest.raises(ExchangeError):
        cancel_order(settings(), "111", "BTC/USDT", factory=lambda *_a, **_k: broker)
    assert broker is not None


# ------------------------------------------------------------------- routes


class EmptyRepository:
    def ping(self) -> bool:
        return True

    def trades(self, limit: int) -> Sequence[Trade]:
        return []

    def audit_events(self, limit: int) -> Sequence[AuditEvent]:
        return []


def client(*, connected: bool) -> TestClient:
    settings_ = settings()
    service = ApiService(
        settings=settings_,
        repository=EmptyRepository(),
        ledger=PortfolioLedger(initial_equity=Decimal("10000")),
        clock=lambda: NOW,
        exchange_probe=lambda: (connected, "testnet unreachable" if not connected else "ok"),
    )
    app = create_app(settings=settings_, service=service)
    return TestClient(app)


def test_api_lists_no_orders_in_paper_mode() -> None:
    response = client(connected=True).get("/api/exchange/orders")

    assert response.status_code == 200
    assert response.json() == {"count": 0, "orders": []}


def test_api_refuses_when_the_exchange_is_offline() -> None:
    response = client(connected=False).get("/api/exchange/orders")

    assert response.status_code == 503
    assert "not connected" in response.json()["detail"]


def test_api_cancel_refuses_when_the_exchange_is_offline() -> None:
    response = client(connected=False).post(
        "/api/exchange/orders/111/cancel", params={"symbol": "BTC/USDT"}
    )

    assert response.status_code == 503


def test_dashboard_page_renders_the_empty_state() -> None:
    page = client(connected=True).get("/exchange-orders", headers={"accept": "text/html"})

    assert page.status_code == 200
    assert "Exchange orders" in page.text
    assert "No open orders on the exchange." in page.text
    assert "Orders" in page.text  # the nav entry


def test_dashboard_page_explains_an_offline_exchange() -> None:
    page = client(connected=False).get("/exchange-orders", headers={"accept": "text/html"})

    assert page.status_code == 200
    assert "Exchange offline" in page.text


def test_ui_cancel_is_refused_when_offline() -> None:
    response = client(connected=False).post(
        "/ui/exchange-orders/111/cancel", params={"symbol": "BTC/USDT"}
    )

    assert response.status_code == 200
    assert "Cancel refused" in response.text


def _unused(_: Any) -> None:  # pragma: no cover - keeps irrelevant imports honest
    return None
