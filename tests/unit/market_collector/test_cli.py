"""CLI wiring: settings -> source -> collector, and the JSON summary shape."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from aegis_config import load_settings
from aegis_core import Timeframe
from market_collector import cli
from market_collector.cli import (
    build_collector,
    build_source_for,
    klines_base_url,
    summary_payload,
)
from market_collector.collector import CollectionSummary, SeriesResult
from market_collector.sources import (
    BINANCE_MAINNET_BASE_URL,
    BINANCE_TESTNET_BASE_URL,
    BinanceKlineSource,
    CsvCandleSource,
)


@contextlib.contextmanager
def fake_session_scope(factory: object) -> Iterator[None]:
    yield None


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def test_paper_settings_select_the_live_source() -> None:
    """Paper mode rehearses the live feed: CSV is an opt-in, not the default."""
    settings = load_settings("paper")

    source = build_source_for(settings)

    assert isinstance(source, BinanceKlineSource)
    assert klines_base_url(settings) == BINANCE_TESTNET_BASE_URL


def test_dev_settings_also_use_the_live_source() -> None:
    settings = load_settings("dev")

    assert isinstance(build_source_for(settings), BinanceKlineSource)
    assert settings.market_data.timeframes == (Timeframe.M15,)


def test_collector_is_built_from_settings() -> None:
    settings = load_settings("paper")

    collector = build_collector(settings, cast(Session, object()))

    assert collector.source_kind == "BinanceKlineSource"


def test_csv_remains_an_explicit_choice() -> None:
    settings = load_settings("paper", overrides={"market_data": {"source": "csv"}})

    source = build_source_for(settings)

    assert isinstance(source, CsvCandleSource)
    assert Path(source.data_dir) == Path("data/seeds")


def test_source_binance_selects_the_live_klines_source() -> None:
    """``AEGIS_MARKET_DATA__SOURCE=binance`` must not fall back to CSV seeds."""
    settings = load_settings("paper", overrides={"market_data": {"source": "binance"}})

    source = build_source_for(settings)

    assert isinstance(source, BinanceKlineSource)
    assert not isinstance(source, CsvCandleSource)
    assert klines_base_url(settings) == BINANCE_TESTNET_BASE_URL  # testnet is the default


def test_the_exchange_name_keeps_working() -> None:
    settings = load_settings("paper", overrides={"market_data": {"source": "exchange"}})

    assert isinstance(build_source_for(settings), BinanceKlineSource)


def test_klines_follow_the_exchange_environment() -> None:
    testnet = load_settings("paper", overrides={"exchange": {"testnet": True}})
    mainnet = load_settings("paper", overrides={"exchange": {"testnet": False}})
    explicit = load_settings(
        "paper",
        overrides={"exchange": {"testnet": False, "base_url": "https://api.binance.com"}},
    )

    assert klines_base_url(testnet) == BINANCE_TESTNET_BASE_URL
    # ``exchange.base_url`` defaults to testnet; ``testnet: false`` must still mean mainnet.
    assert klines_base_url(mainnet) == BINANCE_MAINNET_BASE_URL
    assert klines_base_url(explicit) == "https://api.binance.com"


def test_an_unknown_source_fails_loudly(tmp_path: Path) -> None:
    from market_collector.sources import build_source

    with pytest.raises(ValueError, match="unknown market data source"):
        build_source("yahoo", csv_dir=tmp_path)


def test_summary_payload_is_json_ready() -> None:
    summary = CollectionSummary(
        series=(
            SeriesResult(
                symbol="BTC/USDT",
                timeframe=Timeframe.H1,
                fetched=3,
                inserted=2,
                lag_seconds=300.0,
            ),
            SeriesResult(symbol="ETH/USDT", timeframe=Timeframe.H1, error="boom"),
        ),
        started_at=datetime(2024, 1, 1, tzinfo=UTC),
    )

    payload = summary_payload(summary)

    assert payload["fetched"] == 3
    assert payload["inserted"] == 2
    assert payload["max_lag_seconds"] == 300.0
    assert payload["series"][1]["error"] == "boom"
    assert payload["started_at"] == "2024-01-01T00:00:00+00:00"


class FakeCollector:
    def __init__(self, summary: CollectionSummary) -> None:
        self._summary = summary

    def run_once(self) -> CollectionSummary:
        return self._summary


def fake_summary() -> CollectionSummary:
    return CollectionSummary(
        series=(
            SeriesResult(
                symbol="BTC/USDT",
                timeframe=Timeframe.H1,
                fetched=1,
                inserted=1,
                lag_seconds=60.0,
            ),
            SeriesResult(symbol="ETH/USDT", timeframe=Timeframe.H1, error="boom"),
        ),
        started_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


def test_run_once_prints_json_and_reports_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = fake_summary()
    monkeypatch.setattr(cli, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        cli, "build_collector", lambda settings, session, limit=None: FakeCollector(summary)
    )

    result = cli._run_once(load_settings("paper"), cast(Any, object()), None)

    captured = capsys.readouterr()
    assert result is summary
    assert '"inserted": 1' in captured.out
    assert "ETH/USDT 1h failed: boom" in captured.err


def test_main_once_disposes_the_engine(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = FakeEngine()
    monkeypatch.setattr(cli, "load_settings", lambda: load_settings("paper"))
    monkeypatch.setattr(cli, "create_engine_from_settings", lambda settings: engine)
    monkeypatch.setattr(cli, "create_session_factory", lambda engine: cast(Any, object()))
    monkeypatch.setattr(cli, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        cli, "build_collector", lambda settings, session, limit=None: FakeCollector(fake_summary())
    )

    assert cli.main(["--once"]) == 0

    assert engine.disposed is True
    assert '"fetched": 1' in capsys.readouterr().out
