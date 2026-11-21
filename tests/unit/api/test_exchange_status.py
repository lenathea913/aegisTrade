"""Exchange status: the header badge and the Convert gate when the venue is down."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from aegis_config import load_settings
from aegis_core import Candle, RiskLimit, Timeframe, Trade
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


class Repo:
    def __init__(self, candles: tuple[Candle, ...]) -> None:
        self._candles = candles

    def recent(self, symbol: str, timeframe: Timeframe, limit: int) -> tuple[Candle, ...]:
        return self._candles[-limit:]


def crossover_prefix() -> tuple[Candle, ...]:
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
    all_candles = tuple(candles)
    strategy = EmaCrossStrategy(symbol=SYMBOL, timeframe=TIMEFRAME)
    window = all_candles[-DEFAULT_HISTORY_BARS:]
    if strategy.on_candle(window[-1], window) is None:
        raise AssertionError("fixture produced no crossover in the trailing window")
    return all_candles


def limits() -> RiskLimit:
    return RiskLimit(
        max_risk_per_trade_pct=Decimal("2"),
        max_daily_loss_pct=Decimal("5"),
        max_weekly_loss_pct=Decimal("10"),
        max_exposure_per_symbol_pct=Decimal("100"),
        max_total_exposure_pct=Decimal("100"),
        max_leverage=Decimal("1"),
        allowed_symbols=frozenset({SYMBOL}),
    )


def build_client(probe: Callable[[], tuple[bool, str]]) -> tuple[TestClient, SignalService]:
    settings = load_settings("paper")
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    service = ApiService(
        settings=settings,
        repository=EmptyRepository(),
        ledger=ledger,
        clock=lambda: NOW,
        exchange_probe=probe,
    )
    signals = SignalService(
        gate=RiskGate(limits=limits()),
        ledger=ledger,
        candles=Repo(crossover_prefix()),
        symbols=(SYMBOL,),
        timeframe=TIMEFRAME,
        clock=lambda: crossover_prefix()[-1].close_time + timedelta(seconds=5),
    )
    app = create_app(settings=settings, service=service, signals=signals)
    return TestClient(app), signals


def test_connected_exchange_reports_mode_and_label() -> None:
    client, _ = build_client(lambda: (True, "paper broker: fills are simulated locally"))

    response = client.get("/api/exchange/status")

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["label"] == "PAPER"
    assert body["broker"] == "paper"


def test_header_shows_the_mode_and_a_red_badge_when_offline() -> None:
    client, _ = build_client(lambda: (False, "testnet unreachable"))

    html = client.get("/", headers={"accept": "text/html"})

    assert html.status_code == 200
    assert "PAPER MODE" in html.text
    assert "exchange offline" in html.text


def test_convert_is_disabled_in_the_ui_when_the_exchange_is_down() -> None:
    client, signals = build_client(lambda: (False, "testnet unreachable"))
    signal_id = client.post("/api/signals/generate").json()["signals"][0]["signal_id"]

    response = client.post(f"/ui/signals/{signal_id}/convert")

    assert response.status_code == 200
    assert "Convert disabled" in response.text
    assert "Draft intent" not in response.text
    # The signal is untouched: no conversion happened behind the disabled button.
    assert signals.get(signal_id).status.value == "RISK_OK"


def test_api_conversion_returns_503_when_the_exchange_is_down() -> None:
    client, _ = build_client(lambda: (False, "testnet unreachable"))
    signal_id = client.post("/api/signals/generate").json()["signals"][0]["signal_id"]

    response = client.post(f"/api/signals/{signal_id}/convert")

    assert response.status_code == 503
    assert "not connected" in response.json()["detail"]


def test_a_probe_that_raises_is_reported_as_disconnected() -> None:
    def broken() -> tuple[bool, str]:
        raise RuntimeError("socket exploded")

    client, _ = build_client(broken)

    body = client.get("/api/exchange/status").json()

    assert body["connected"] is False
    assert "socket exploded" in body["detail"]
