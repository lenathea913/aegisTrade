"""Shared test factories and fixtures.

Domain factories live here so every module can build valid objects and override a
single field to probe one invariant. The risk fixtures baseline is a *clean
approval*:

* equity 10 000, BTC/USDT quote 30 000 five seconds old
* intent: 0.1 BTC @ 30 000, stop 29 000 -> risk 100 (1 % of equity), notional 3 000
* limits: 2 % risk/trade, 5 % daily, 10 % weekly, 50 % per symbol, 100 % total, 1x
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from aegis_config.settings import Settings
from aegis_core import (
    ApprovedOrder,
    MarketContext,
    MarketQuote,
    OrderIntent,
    OrderType,
    PortfolioState,
    RiskLimit,
    Side,
)
from aegis_risk import RiskGate

#: Tests must be reproducible on a developer machine that has a populated ``.env``
#: (live keys, ``market_data.source=binance``, alerting on). The suite asserts the
#: committed YAML defaults, so the dotenv source is disabled for the whole session.
_settings_config = Settings.model_config
_original_env_file = _settings_config.get("env_file")


@pytest.fixture(autouse=True, scope="session")
def _ignore_local_dotenv() -> Iterator[None]:
    _settings_config["env_file"] = None
    try:
        yield
    finally:
        _settings_config["env_file"] = _original_env_file


@pytest.fixture
def intent_kwargs() -> dict[str, Any]:
    """A valid BUY LIMIT intent: 0.5 BTC/USDT @ 30 000, stop 29 000."""
    return {
        "symbol": "BTC/USDT",
        "side": Side.BUY,
        "order_type": OrderType.LIMIT,
        "qty": Decimal("0.5"),
        "price": Decimal("30000"),
        "stop_loss": Decimal("29000"),
        "take_profit": Decimal("32000"),
        "strategy_id": "simple_ema_cross",
        "reason": "ema(12) crossed above ema(26)",
    }


@pytest.fixture
def make_intent(intent_kwargs: dict[str, Any]) -> Callable[..., OrderIntent]:
    def _make(**overrides: Any) -> OrderIntent:
        return OrderIntent(**{**intent_kwargs, **overrides})

    return _make


@pytest.fixture
def approved(make_intent: Callable[..., OrderIntent]) -> ApprovedOrder:
    return ApprovedOrder(intent=make_intent(), max_risk_amount=Decimal("500"))


# --------------------------------------------------------------------- risk


@pytest.fixture
def decision_now() -> datetime:
    return datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def limits() -> RiskLimit:
    return RiskLimit(
        max_risk_per_trade_pct=Decimal("2"),
        max_daily_loss_pct=Decimal("5"),
        max_weekly_loss_pct=Decimal("10"),
        max_exposure_per_symbol_pct=Decimal("50"),
        max_total_exposure_pct=Decimal("100"),
        max_leverage=Decimal("1"),
        allowed_symbols=frozenset({"BTC/USDT"}),
    )


@pytest.fixture
def market(decision_now: datetime) -> MarketContext:
    return MarketContext(
        quotes=(
            MarketQuote(
                symbol="BTC/USDT",
                price=Decimal("30000"),
                as_of=decision_now - timedelta(seconds=5),
            ),
        ),
        now=decision_now,
    )


@pytest.fixture
def portfolio() -> PortfolioState:
    return PortfolioState(equity=Decimal("10000"))


@pytest.fixture
def small_intent(make_intent: Callable[..., OrderIntent]) -> OrderIntent:
    """0.1 BTC @ 30 000 with a 1 000 stop -> 100 of risk on 10 000 equity."""
    return make_intent(qty=Decimal("0.1"))


# ---------------------------------------------------------------- execution


@pytest.fixture
def now() -> datetime:
    """Deterministic decision clock shared by execution tests."""
    return datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def market_at(now: datetime) -> MarketContext:
    """BTC/USDT at 30 000, five seconds old."""
    return MarketContext(
        quotes=(
            MarketQuote(
                symbol="BTC/USDT", price=Decimal("30000"), as_of=now - timedelta(seconds=5)
            ),
        ),
        now=now,
    )


@pytest.fixture
def gate(limits: RiskLimit) -> RiskGate:
    return RiskGate(limits=limits, stale_after_seconds=60)


@pytest.fixture
def make_approved(make_intent: Callable[..., OrderIntent]) -> Callable[..., ApprovedOrder]:
    """An approval whose timestamps are consistent with the ``now`` fixture."""

    def _make(**overrides: Any) -> ApprovedOrder:
        moment = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        intent = make_intent(created_at=moment - timedelta(seconds=1), **overrides)
        return ApprovedOrder(
            intent=intent,
            risk_checks_passed=("risk_per_trade_within_limit",),
            max_risk_amount=Decimal("200"),
            approved_at=moment,
        )

    return _make
