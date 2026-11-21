"""Dashboard pages, HTMX partials and the signal API over the same app."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from aegis_config import MarketType, load_settings
from aegis_core import (
    TRADER_MOTIVATION_QUOTE,
    Candle,
    RiskLimit,
    Side,
    SignalStatus,
    Timeframe,
    Trade,
)
from aegis_db import ActivityRecord
from aegis_ledger import AuditEvent, PortfolioLedger
from aegis_risk import RiskGate
from aegis_strategy import EmaCrossStrategy
from api.app import create_app
from api.service import ApiService
from signal_service import DEFAULT_HISTORY_BARS, SignalService

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
SYMBOL = "BTC/USDT"
TIMEFRAME = Timeframe.H1


class EmptyRepository:
    def ping(self) -> bool:
        return True

    def reconstruct(self, ledger: PortfolioLedger, *, strict: bool = True) -> None:
        """Offline fake: nothing is stored, so the book keeps what the test set."""

    def trades(self, limit: int) -> Sequence[Trade]:
        return []

    def audit_events(self, limit: int) -> Sequence[AuditEvent]:
        return []


def crossover_history() -> tuple[Candle, ...]:
    price = Decimal("40000")
    moment = datetime(2026, 6, 1, tzinfo=UTC)
    candles: list[Candle] = []
    for index in range(398):
        step = Decimal("-0.001") if index < 320 else Decimal("0.0015")
        if index >= 394:
            # End on a cross for whatever EMA pair the strategy currently defaults to
            # (3/5 during the stress test, 50/200 before it): dip three bars, then pop.
            step = Decimal("-0.002") if index < 397 else Decimal("0.004")
        open_price = price
        close_price = price * (Decimal("1") + step)
        candles.append(
            Candle(
                symbol=SYMBOL,
                timeframe=TIMEFRAME,
                open_time=moment,
                close_time=moment + timedelta(hours=1),
                open=open_price,
                high=max(open_price, close_price),
                low=min(open_price, close_price),
                close=close_price,
                volume=Decimal("10"),
            )
        )
        price = close_price
        moment += timedelta(hours=1)
    return tuple(candles)


def crossover_prefix() -> tuple[Candle, ...]:
    candles = crossover_history()
    strategy = EmaCrossStrategy(symbol=SYMBOL, timeframe=TIMEFRAME)
    window = candles[-DEFAULT_HISTORY_BARS:]
    if strategy.on_candle(window[-1], window) is None:
        raise AssertionError("fixture produced no crossover in the trailing window")
    return candles


class EmptyActivity:
    """The activity page reads the feed, not the trading repository."""

    def recent(self, limit: int) -> tuple[ActivityRecord, ...]:
        return ()


class Repo:
    def __init__(self, candles: tuple[Candle, ...]) -> None:
        self._candles = candles

    def recent(self, symbol: str, timeframe: Timeframe, limit: int) -> tuple[Candle, ...]:
        return self._candles[-limit:]


def limits(**overrides: object) -> RiskLimit:
    values: dict[str, object] = {
        "max_risk_per_trade_pct": Decimal("2"),
        "max_daily_loss_pct": Decimal("5"),
        "max_weekly_loss_pct": Decimal("10"),
        "max_exposure_per_symbol_pct": Decimal("100"),
        "max_total_exposure_pct": Decimal("100"),
        "max_leverage": Decimal("1"),
        "allowed_symbols": frozenset({SYMBOL}),
    }
    values.update(overrides)
    return RiskLimit(**values)


def no_cross_prefix() -> tuple[Candle, ...]:
    """History whose **last** bar has no crossover: the normal empty scan."""
    candles = crossover_history()
    for end in range(len(candles) - 1, DEFAULT_HISTORY_BARS, -1):
        window = candles[end - DEFAULT_HISTORY_BARS : end]
        strategy = EmaCrossStrategy(symbol=SYMBOL, timeframe=TIMEFRAME)
        if strategy.on_candle(window[-1], window) is None:
            return tuple(candles[:end])
    raise AssertionError("fixture always crosses")


def build_client(
    *,
    risk_gate: RiskGate | None = None,
    candles: tuple[Candle, ...] | None = None,
    signals_override: Any = None,
) -> tuple[TestClient, SignalService]:
    settings = load_settings("paper")
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    service = ApiService(
        settings=settings,
        repository=EmptyRepository(),
        ledger=ledger,
        clock=lambda: NOW,
    )
    history = crossover_prefix() if candles is None else candles
    signals = signals_override or SignalService(
        gate=risk_gate or RiskGate(limits=limits()),
        ledger=ledger,
        candles=Repo(history),
        symbols=(SYMBOL,),
        timeframe=TIMEFRAME,
        clock=lambda: history[-1].close_time + timedelta(seconds=5) if history else NOW,
    )
    app = create_app(settings=settings, service=service, signals=signals)
    return TestClient(app), signals


def test_motivation_endpoint_returns_a_documented_quote() -> None:
    client, _ = build_client()

    response = client.get("/motivation")

    assert response.status_code == 200
    assert response.json()["quote"] in TRADER_MOTIVATION_QUOTE


def test_pages_render_dark_dashboard() -> None:
    client, _ = build_client()

    html = {"accept": "text/html"}
    overview = client.get("/", headers=html)
    positions = client.get("/positions", headers=html)
    journal = client.get("/journal", headers=html)
    signals = client.get("/signals", headers=html)

    assert overview.status_code == 200
    assert "Overview" in overview.text and "Equity" in overview.text
    assert "dark" in overview.text and "tailwindcss.com" in overview.text
    assert positions.status_code == 200 and "No open positions" in positions.text
    assert journal.status_code == 200 and "append-only" in journal.text
    assert signals.status_code == 200 and "Scan candles" in signals.text


def test_the_market_badge_follows_the_configuration_on_every_page() -> None:
    """A page that forgets ``health`` used to fall back to the template's "spot"."""
    base = load_settings("paper")
    futures = base.model_copy(
        update={"execution": base.execution.model_copy(update={"market_type": MarketType.FUTURES})}
    )
    service = ApiService(
        settings=futures,
        repository=EmptyRepository(),
        ledger=PortfolioLedger(initial_equity=Decimal("10000")),
        clock=lambda: NOW,
    )
    client = TestClient(create_app(settings=futures, service=service, activity=EmptyActivity()))

    for path in ("/", "/positions", "/journal", "/activity"):
        page = client.get(path, headers={"accept": "text/html"})
        assert page.status_code == 200, path
        assert "FUTURES" in page.text, path
        assert "bg-indigo-500/20" in page.text, path


def test_partials_serve_htmx_polling() -> None:
    client, _ = build_client()

    for path in (
        "/partials/overview",
        "/partials/positions",
        "/partials/signals",
        "/partials/motivation",
    ):
        response = client.get(path)
        assert response.status_code == 200, path


def test_the_scan_button_says_when_the_latest_bar_has_no_cross() -> None:
    client, _ = build_client(candles=no_cross_prefix())

    response = client.post("/ui/signals/generate")

    assert response.status_code == 200
    # Silence was the bug: the button looked broken while nothing was wrong.
    assert "has not crossed" in response.text
    assert "Scanning again reads the same bar" in response.text
    assert "No signals yet" in response.text


def test_the_scan_button_says_when_there_is_not_enough_history() -> None:
    client, _ = build_client(candles=())

    response = client.post("/ui/signals/generate")

    assert response.status_code == 200
    assert "stored candles" in response.text
    assert "needs" in response.text


def test_the_scan_button_reports_a_database_failure_in_the_page() -> None:
    class BrokenSignals:
        def scan(self) -> Any:
            raise SQLAlchemyError("connection refused")

        def list_signals(self) -> Any:  # pragma: no cover - never reached
            raise AssertionError("list must not be called after a failed scan")

    client, _ = build_client(signals_override=BrokenSignals())

    response = client.post("/ui/signals/generate")

    # An HTMX swap needs 200: a 500 leaves the old list on screen with no explanation.
    assert response.status_code == 200
    assert "Signal scan failed" in response.text
    assert "database is not reachable" in response.text


def test_signal_api_lifecycle() -> None:
    client, _ = build_client()

    generated = client.post("/api/signals/generate")
    assert generated.status_code == 200
    signal = generated.json()["signals"][0]
    assert signal["status"] == SignalStatus.RISK_OK.value

    detail = client.get(f"/api/signals/{signal['signal_id']}")
    assert detail.status_code == 200
    assert detail.json()["risk_reward"] == signal["risk_reward"]

    converted = client.post(f"/api/signals/{signal['signal_id']}/convert")
    assert converted.status_code == 200
    body = converted.json()
    assert body["status"] == "DRAFT"
    assert body["intent"]["side"] == "BUY"
    assert body["signal"]["status"] == SignalStatus.CONVERTED.value
    assert body["max_risk_amount"]

    ignored = client.post(f"/api/signals/{signal['signal_id']}/ignore")
    assert ignored.status_code == 409  # a converted signal cannot be ignored


def test_conversion_of_a_blocked_signal_returns_400_with_reasons() -> None:
    tight = RiskGate(limits=limits(allowed_symbols=frozenset()))
    client, signals = build_client(risk_gate=tight)

    generated = client.post("/api/signals/generate").json()["signals"][0]
    assert generated["status"] == SignalStatus.BLOCKED_BY_RISK.value

    response = client.post(f"/api/signals/{generated['signal_id']}/convert")

    assert response.status_code == 400
    assert response.json()["reasons"]
    assert signals.get(generated["signal_id"]).status is SignalStatus.BLOCKED_BY_RISK


def test_unknown_signal_returns_404() -> None:
    client, _ = build_client()

    assert client.get("/api/signals/sig_nope").status_code == 404
    assert client.post("/api/signals/sig_nope/convert").status_code == 404


def test_ui_convert_swaps_in_a_draft_and_shows_rejection() -> None:
    client, _ = build_client()
    signal_id = client.post("/api/signals/generate").json()["signals"][0]["signal_id"]

    converted = client.post(f"/ui/signals/{signal_id}/convert")
    assert converted.status_code == 200
    assert "Draft intent" in converted.text and "not executed" in converted.text

    again = client.post(f"/ui/signals/{signal_id}/convert")
    assert again.status_code == 200
    assert "already converted" in again.text


def test_convert_is_refused_while_a_position_is_open() -> None:
    """The manual path must not pyramid either: 409, and the button is disabled."""
    client, signals = build_client()
    signal = client.post("/api/signals/generate").json()["signals"][0]

    ledger = cast(Any, client.app).state.service.ledger
    ledger.apply_trade(
        Trade(
            order_id="ord_open",
            symbol=SYMBOL,
            side=Side.BUY,
            qty=Decimal("0.06"),
            price=Decimal("30000"),
            fee=Decimal("0"),
            filled_at=NOW,
            strategy_id="simple_ema_cross",
        )
    )

    response = client.post(f"/api/signals/{signal['signal_id']}/convert")

    assert response.status_code == 409
    assert response.json()["detail"] == "Cannot convert: position already open in this direction."
    assert signals.get(signal["signal_id"]).status is SignalStatus.RISK_OK

    detail = client.get(f"/partials/signals/{signal['signal_id']}")
    assert "position already open" in detail.text
    assert "disabled" in detail.text

    listing = client.get("/partials/signals")
    assert "position open" in listing.text


def test_convert_succeeds_after_the_position_is_flat() -> None:
    client, _ = build_client()
    signal = client.post("/api/signals/generate").json()["signals"][0]
    ledger = cast(Any, client.app).state.service.ledger
    ledger.apply_trade(
        Trade(
            order_id="ord_open",
            symbol=SYMBOL,
            side=Side.BUY,
            qty=Decimal("0.06"),
            price=Decimal("30000"),
            fee=Decimal("0"),
            filled_at=NOW,
            strategy_id="simple_ema_cross",
        )
    )
    ledger.apply_trade(
        Trade(
            order_id="ord_close",
            symbol=SYMBOL,
            side=Side.SELL,
            qty=Decimal("0.06"),
            price=Decimal("30000"),
            fee=Decimal("0"),
            filled_at=NOW,
            strategy_id="position_monitor",
        )
    )

    response = client.post(f"/api/signals/{signal['signal_id']}/convert")

    assert response.status_code == 200
    assert response.json()["status"] == "DRAFT"


def test_positions_page_offers_a_manual_close() -> None:
    """The operator exit exists in the UI; without a session factory it reports why."""
    client, _ = build_client()
    ledger = cast(Any, client.app).state.service.ledger
    ledger.apply_trade(
        Trade(
            order_id="ord_open_ui",
            symbol=SYMBOL,
            side=Side.BUY,
            qty=Decimal("0.06"),
            price=Decimal("30000"),
            fee=Decimal("0"),
            filled_at=NOW,
            strategy_id="simple_ema_cross",
        )
    )

    body = client.get("/positions", headers={"accept": "text/html"}).text

    assert "Close Position" in body
    assert "/ui/positions/BTC/USDT/close" in body
    assert "Close the whole BTC/USDT position" in body

    response = client.post("/ui/positions/BTC/USDT/close")

    assert response.status_code == 200
    assert "trading is not configured" in response.text


def test_journal_page_rounds_money_instead_of_printing_raw_decimals() -> None:
    """The regression from the first paper run: 0.03065... must render as 0.03."""
    trade = Trade(
        order_id="ord-1",
        symbol=SYMBOL,
        side=Side.BUY,
        qty=Decimal("0.030000000000000001"),
        price=Decimal("30015.000000000004"),
        fee=Decimal("0.0045022500000000002"),
        filled_at=NOW,
        strategy_id="simple_ema_cross",
    )

    class JournalRepository(EmptyRepository):
        def trades(self, limit: int) -> Sequence[Trade]:
            return [trade]

    settings = load_settings("paper")
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    service = ApiService(
        settings=settings,
        repository=JournalRepository(),
        ledger=ledger,
        clock=lambda: NOW,
    )
    client = TestClient(create_app(settings=settings, service=service))

    body = client.get("/journal", headers={"accept": "text/html"}).text

    assert "BUY 0.03 BTC/USDT @ 30015.00" in body
    assert '"fee": "0.00450225"' in body
    assert "0.0045022500000000002" not in body
    assert "0.030000000000000001" not in body


def test_ui_ignore_marks_the_signal() -> None:
    client, _ = build_client()
    signal_id = client.post("/api/signals/generate").json()["signals"][0]["signal_id"]

    response = client.post(f"/ui/signals/{signal_id}/ignore")

    assert response.status_code == 200
    assert "IGNORED" in response.text
