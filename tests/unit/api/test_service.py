"""Read-side service: health, positions, risk budget and the merged journal."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import SQLAlchemyError

from aegis_config import Settings, load_settings
from aegis_core import MarketQuote, Side, Trade
from aegis_ledger import (
    AuditEvent,
    EventType,
    LedgerError,
    PortfolioLedger,
    PositionProtection,
    RealizedEvent,
)
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

    def reconstruct(self, ledger: PortfolioLedger, *, strict: bool = True) -> None:
        """Offline fakes hold no durable record, so there is nothing to replay."""


class BookRepository(FakeRepository):
    """A stand-in for the database the runner also writes to.

    ``reconstruct`` mirrors what PostgreSQL does: drop the in-memory book and replay the
    stored trades. Mutating ``stored_trades`` between reads is how a test plays the part
    of the runner filling an order.
    """

    def __init__(self, *, database_ok: bool = True) -> None:
        super().__init__(database_ok=database_ok)
        self.stored_trades: list[Trade] = []
        self.reconstructions = 0
        self.strict_flags: list[bool] = []

    def reconstruct(self, ledger: PortfolioLedger, *, strict: bool = True) -> None:
        self.reconstructions += 1
        self.strict_flags.append(strict)
        ledger.reset()
        for stored in self.stored_trades:
            ledger.apply_trade(stored)


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


class Marks:
    """A live-mark source: symbol -> price, optionally raising."""

    def __init__(self, prices: dict[str, str], *, error: Exception | None = None) -> None:
        self._prices = {symbol: Decimal(price) for symbol, price in prices.items()}
        self._error = error
        self.reads: list[str] = []

    def latest(self, symbol: str) -> MarketQuote | None:
        self.reads.append(symbol)
        if self._error is not None:
            raise self._error
        price = self._prices.get(symbol)
        if price is None:
            return None
        return MarketQuote(symbol=symbol, price=price, as_of=NOW)


def make_service(
    *,
    repository: FakeRepository | None = None,
    ledger: PortfolioLedger | None = None,
    settings: Settings | None = None,
    marks: Marks | None = None,
) -> ApiService:
    return ApiService(
        settings=settings or load_settings("paper"),
        repository=repository or FakeRepository(),
        ledger=ledger or PortfolioLedger(initial_equity=Decimal("10000")),
        clock=lambda: NOW,
        marks=marks,
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


def test_positions_are_rebuilt_from_the_database_not_a_startup_snapshot() -> None:
    """A fill the runner writes in another process must show up without a restart."""
    repository = BookRepository()
    service = make_service(repository=repository)
    assert service.positions().count == 0

    # The runner trades; this process is neither restarted nor told about it.
    repository.stored_trades.append(trade(qty="0.5", price="30000"))

    positions = service.refresh_book(force=True).snapshot(now=NOW).open_positions

    assert [position.symbol for position in positions] == ["BTC/USDT"]
    assert Decimal(positions[0].qty) == Decimal("0.5")


def test_the_book_is_replayed_once_per_poll_window() -> None:
    repository = BookRepository()
    service = make_service(repository=repository)

    service.positions()
    service.risk_budget()

    assert repository.reconstructions == 1
    assert repository.strict_flags == [False]


def test_live_marks_drive_unrealized_pnl_and_equity() -> None:
    """A replayed book only knows entry prices; the live mark is what makes PnL real."""
    repository = BookRepository()
    repository.stored_trades.append(trade(qty="0.5", price="30000"))
    marks = Marks({"BTC/USDT": "31000"})

    positions = make_service(repository=repository, marks=marks).positions()
    budget = make_service(repository=repository, marks=marks).risk_budget()

    position = positions.positions[0]
    assert Decimal(position.unrealized_pnl) == Decimal("500")
    assert Decimal(position.roe_pct) == Decimal("500") / Decimal("15000") * 100
    assert Decimal(budget.equity) == Decimal("10500")
    assert Decimal(budget.total_exposure) == Decimal("15500")
    assert marks.reads.count("BTC/USDT") == 2


def test_a_missing_live_mark_keeps_the_replayed_mark() -> None:
    repository = BookRepository()
    repository.stored_trades.append(trade(qty="0.5", price="30000"))

    positions = make_service(repository=repository, marks=Marks({})).positions()

    assert Decimal(positions.positions[0].unrealized_pnl) == Decimal("0")
    assert Decimal(positions.positions[0].avg_entry) == Decimal("30000")


def test_a_failing_mark_source_does_not_break_the_page() -> None:
    repository = BookRepository()
    repository.stored_trades.append(trade(qty="0.5", price="30000"))

    positions = make_service(
        repository=repository, marks=Marks({}, error=ConnectionError("redis is down"))
    ).positions()

    assert positions.count == 1
    assert Decimal(positions.positions[0].unrealized_pnl) == Decimal("0")


def test_protection_status_separates_venue_book_and_naked() -> None:
    """A paper fill has no venue id but does have a stop: it is not naked."""
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(trade())
    ledger.set_protection(
        PositionProtection(
            symbol="BTC/USDT",
            side=Side.BUY,
            qty=Decimal("0.5"),
            stop_loss=Decimal("29000"),
            take_profit=Decimal("32000"),
        )
    )

    book = make_service(ledger=ledger).positions().positions[0]
    assert book.protection == "BOOK_PROTECTED"
    assert book.protected is True
    assert book.stop_loss == "29000"

    ledger.set_protection(
        PositionProtection(
            symbol="BTC/USDT",
            side=Side.BUY,
            qty=Decimal("0.5"),
            stop_loss=Decimal("29000"),
            stop_order_id="venue-stop-1",
        )
    )
    venue = make_service(ledger=ledger).positions().positions[0]
    assert venue.protection == "VENUE_PROTECTED"
    assert venue.stop_order_id == "venue-stop-1"

    ledger.clear_protection("BTC/USDT")
    naked = make_service(ledger=ledger).positions().positions[0]
    assert naked.protection == "NAKED"
    assert naked.protected is False


def test_equity_is_recomputed_from_the_trades_in_the_database() -> None:
    repository = BookRepository()
    repository.stored_trades.extend([trade(fee="2"), trade(fee="3")])

    budget = make_service(repository=repository).risk_budget()

    # 10 000 to start, two fills costing 2 and 3; the position is marked at its own fill,
    # so the fees are the only realized change.
    assert Decimal(budget.equity) == Decimal("9995")
    assert budget.open_positions == 1
    assert Decimal(budget.total_exposure) == Decimal("30000")


def test_a_failed_refresh_keeps_the_last_good_book_instead_of_blanking_it() -> None:
    class BrokenBook(BookRepository):
        def reconstruct(self, ledger: PortfolioLedger, *, strict: bool = True) -> None:
            raise SQLAlchemyError("database is down")

    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(trade())

    positions = make_service(repository=BrokenBook(), ledger=ledger).positions()

    assert positions.count == 1


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
    assert budget.limits.allowed_symbols == [
        "1000PEPE/USDT",
        "BNB/USDT",
        "BTC/USDT",
        "DOGE/USDT",
        "ETH/USDT",
        "HYPE/USDT",
        "NEAR/USDT",
        "SOL/USDT",
        "XRP/USDT",
        "ZEC/USDT",
    ]
    assert budget.limits.max_open_positions == 3
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
    assert journal.entries[1].title == "BUY 0.5 BTC/USDT @ 30000.00"
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
