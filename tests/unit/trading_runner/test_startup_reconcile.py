"""Startup reconciliation: no work for paper, and no crash when the venue is down."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from aegis_config import Settings, load_settings
from aegis_ledger import PortfolioLedger
from aegis_observability import configure_logging
from trading_runner import cli


def test_paper_mode_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings("paper")
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    called: list[str] = []

    def fail(*_args: object, **_kwargs: object) -> None:
        called.append("boom")

    monkeypatch.setattr(cli, "reconcile_once", fail)
    cli.reconcile_at_startup(settings, None, ledger)  # type: ignore[arg-type]

    assert called == []


def test_a_live_run_reconciles_before_the_first_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings("paper").model_copy(
        update={
            "execution": load_settings("paper").execution.model_copy(
                update={"broker": "binance-testnet"}
            )
        }
    )
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    called: list[Settings] = []

    def fake(
        settings_: Settings, _factory: sessionmaker[Session], _ledger: PortfolioLedger
    ) -> tuple[object, ...]:
        called.append(settings_)
        return ()

    configure_logging(settings.observability)
    monkeypatch.setattr(cli, "reconcile_once", fake)
    cli.reconcile_at_startup(settings, None, ledger)  # type: ignore[arg-type]

    assert called == [settings]


def test_a_failing_venue_does_not_stop_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings("paper").model_copy(
        update={
            "execution": load_settings("paper").execution.model_copy(
                update={"broker": "binance-testnet"}
            )
        }
    )
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("exchange unreachable")

    configure_logging(settings.observability)
    monkeypatch.setattr(cli, "reconcile_once", boom)
    cli.reconcile_at_startup(settings, None, ledger)  # type: ignore[arg-type]
