"""Read-side service: health, positions, risk budget and the merged journal."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aegis_config import Settings, load_settings
from aegis_core import Side, Trade
from aegis_ledger import AuditEvent, EventType, LedgerError, PortfolioLedger, RealizedEvent
from api.schemas import JournalResponse, PositionsResponse, RiskBudgetResponse
from api.service import ApiService

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class FakeRepository:
    def __init__(
        self,
        *,
        trades: Sequence[Trade] = (),
        events: Sequence[AuditEvent] = (),
        database_ok: bool = True,
    ) -> None:
        self._trades = tuple(trades)
        self._events = tuple(events)
        self.database_ok = database_ok
        self.limits: list[int] = []

    def ping(self) -> bool:
        return self.database_ok

    def trades(self, limit: int) -> Sequence[Trade]:
        self.limits.append(limit)
        return self._trades[:limit]

    def audit_events(self, limit: int) -> Sequence[AuditEvent]:
        self.limits.append(limit)
        return self._events[:limit]


def trade(
    *,
    at: datetime = NOW,
    price: str = "30000",
    side: Side = Side.BUY,
    fee: str = "0",
    qty: str = "0.5",
) -> Trade:
    return Trade(
        order_id="ord-1",
        symbol="BTC/USDT",
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        filled_at=at,
        strategy_id="simple_ema_cross",
    )


def event(kind: EventType, at: datetime = NOW) -> AuditEvent:
    return AuditEvent(event_type=kind, entity_type="order", entity_id="ord-1", created_at=at)


def make_service(
    *,
    repository: FakeRepository | None = None,
    ledger: PortfolioLedger | None = None,
    settings: Settings | None = None,
) -> ApiService:
    return ApiService(
        settings=settings or load_settings("paper"),
        repository=repository or FakeRepository(),
        ledger=ledger or PortfolioLedger(initial_equity=Decimal("10000")),
        clock=lambda: NOW,
    )


# --------------------------------------------------------------------- health


def test_health_reports_the_running_configuration() -> None:
    health = make_service().health()

    assert health.status == "ok"
    assert health.database == "ok"
    assert health.environment == "paper"
    assert health.execution_mode == "paper"
    assert health.kill_switch is False
    assert health.time == NOW
    assert health.version


def test_health_degrades_when_the_database_is_down() -> None:
    health = make_service(repository=FakeRepository(database_ok=False)).health()

    assert health.status == "degraded"
    assert health.database == "unavailable"


def test_health_is_cached_so_polling_does_not_repeat_the_probe() -> None:
    repository = FakeRepository()
    service = make_service(repository=repository)
    probes = 0

    original = repository.ping

    def counting_ping() -> bool:
        nonlocal probes
        probes += 1
        return original()

    repository.ping = counting_ping  # type: ignore[method-assign]

    assert service.health().status == "ok"
    assert service.health().status == "ok"

    assert probes == 1


def test_health_surfaces_the_kill_switch() -> None:
    settings = load_settings("paper")
    halted = settings.model_copy(
        update={"risk": settings.risk.model_copy(update={"kill_switch": True})}
    )

    assert make_service(settings=halted).health().kill_switch is True


# ------------------------------------------------------------------ positions


def test_positions_are_empty_on_a_fresh_ledger() -> None:
    positions = make_service().positions()

    assert isinstance(positions, PositionsResponse)
    assert positions.count == 0
    assert positions.positions == []


def test_positions_are_marked_and_serialised_as_strings() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(trade())
    ledger.mark_price("BTC/USDT", Decimal("31000"))

    positions = make_service(ledger=ledger).positions()

    assert positions.count == 1
    position = positions.positions[0]
    assert position.symbol == "BTC/USDT"
    assert position.side == "BUY"
    assert Decimal(position.qty) == Decimal("0.5")
    assert Decimal(position.avg_entry) == Decimal("30000")
    assert Decimal(position.notional) == Decimal("15000")
    assert Decimal(position.unrealized_pnl) == Decimal("500")
    assert Decimal(position.realized_pnl) == Decimal("0")


# ---------------------------------------------------------------- risk budget


def test_budget_of_a_flat_account() -> None:
    budget = make_service().risk_budget()

    assert isinstance(budget, RiskBudgetResponse)
    assert Decimal(budget.equity) == Decimal("10000")
    assert Decimal(budget.risk_per_trade_capacity) == Decimal("50")
    assert Decimal(budget.daily_loss_limit) == Decimal("200")
    assert Decimal(budget.daily_loss_remaining) == Decimal("200")
    assert Decimal(budget.weekly_loss_limit) == Decimal("500")
    assert Decimal(budget.weekly_loss_remaining) == Decimal("500")
    assert Decimal(budget.total_exposure) == Decimal("0")
    assert Decimal(budget.total_exposure_pct) == Decimal("0")
    assert Decimal(budget.max_total_exposure) == Decimal("6000")
    assert Decimal(budget.leverage) == Decimal("0")
    assert budget.open_positions == 0
    assert budget.kill_switch is False
    assert budget.limits.allowed_symbols == ["BTC/USDT"]
    assert Decimal(budget.limits.max_risk_per_trade_pct) == Decimal("0.5")


def test_realized_loss_reduces_the_remaining_budget() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.restore([], [RealizedEvent(Decimal("-50"), NOW, "trd-1", "BTC/USDT")])

    budget = make_service(ledger=ledger).risk_budget()

    equity = Decimal("9950")
    assert Decimal(budget.equity) == equity
    assert Decimal(budget.daily_realized_pnl) == Decimal("-50")
    assert Decimal(budget.daily_loss_limit) == equity * Decimal("2") / 100
    assert Decimal(budget.daily_loss_remaining) == equity * Decimal("2") / 100 - 50
    assert Decimal(budget.weekly_loss_remaining) == equity * Decimal("5") / 100 - 50


def test_open_exposure_is_reported_and_marked() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(trade(qty="0.1"))
    ledger.mark_price("BTC/USDT", Decimal("31000"))

    budget = make_service(ledger=ledger).risk_budget()

    equity = Decimal("10100")
    exposure = Decimal("3100")
    assert Decimal(budget.equity) == equity
    assert Decimal(budget.total_exposure) == exposure
    assert Decimal(budget.total_exposure_pct) == exposure / equity * 100
    assert Decimal(budget.leverage) == exposure / equity
    assert budget.open_positions == 1


def test_positions_refuse_a_depleted_ledger() -> None:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(trade())
    ledger.mark_price("BTC/USDT", Decimal("1"))

    with pytest.raises(LedgerError, match="equity depleted"):
        make_service(ledger=ledger).positions()


# --------------------------------------------------------------------- journal


def test_journal_merges_trades_and_audit_events_newest_first() -> None:
    repository = FakeRepository(
        trades=[trade(at=NOW - timedelta(minutes=5)), trade(at=NOW - timedelta(minutes=1))],
        events=[
            event(EventType.ORDER_APPROVED, at=NOW - timedelta(minutes=2)),
            event(EventType.TRADE_RECORDED, at=NOW),
        ],
    )

    journal = make_service(repository=repository).journal(limit=10)

    assert isinstance(journal, JournalResponse)
    assert journal.count == 4
    assert [entry.kind for entry in journal.entries] == ["audit", "trade", "audit", "trade"]
    assert journal.entries[0].at == NOW
    assert journal.entries[1].title == "BUY 0.5 BTC/USDT @ 30000"
    assert journal.entries[1].detail["fee"] == "0"
    assert journal.entries[0].title == "trade_recorded"
    assert journal.entries[0].detail["entity_id"] == "ord-1"
    assert repository.limits == [10, 10]


def test_journal_honours_the_limit() -> None:
    repository = FakeRepository(
        trades=[trade(at=NOW - timedelta(minutes=n)) for n in range(1, 4)],
        events=[
            event(EventType.ORDER_APPROVED, at=NOW - timedelta(minutes=n)) for n in range(4, 7)
        ],
    )

    journal = make_service(repository=repository).journal(limit=2)

    assert journal.limit == 2
    assert journal.count == 2
    assert len(journal.entries) == 2
