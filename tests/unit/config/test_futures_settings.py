"""Futures configuration: the hard 5x cap and the testnet-only rule."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from aegis_config import ExecutionSettings, MarketType, Settings
from aegis_config.settings import ExecutionMode


def settings_with(**overrides: Any) -> Settings:
    """A minimal Settings tree, built without reading ``.env`` or YAML."""
    values: dict[str, Any] = {
        "environment": "paper",
        "database": {"url": "postgresql+psycopg://user:pass@localhost:5432/aegis"},
        "redis": {"url": "redis://localhost:6379/0"},
    }
    values.update(overrides)
    return Settings(**values)


def test_market_type_defaults_to_spot() -> None:
    settings = settings_with()

    assert settings.execution.market_type is MarketType.SPOT
    assert settings.execution.max_leverage == 5


@pytest.mark.parametrize("leverage", [0, 6, 20])
def test_the_leverage_ceiling_cannot_be_configured_above_five(leverage: int) -> None:
    with pytest.raises(ValidationError):
        ExecutionSettings(max_leverage=leverage)


def test_futures_is_allowed_on_the_testnet() -> None:
    settings = settings_with(execution={"market_type": "futures", "max_leverage": 3})

    assert settings.execution.market_type is MarketType.FUTURES
    assert settings.execution.max_leverage == 3


def test_futures_refuses_a_mainnet_venue() -> None:
    with pytest.raises(ValidationError, match="testnet"):
        settings_with(execution={"market_type": "futures"}, exchange={"testnet": False})


def test_futures_refuses_the_live_environment() -> None:
    with pytest.raises(ValidationError, match="testnet"):
        settings_with(environment="live", execution={"market_type": "futures"})


def test_futures_refuses_live_execution_mode() -> None:
    with pytest.raises(ValidationError, match="testnet-only"):
        ExecutionSettings(
            mode=ExecutionMode.LIVE,
            broker="binance-testnet",
            allow_live_orders=True,
            market_type=MarketType.FUTURES,
        )


def test_paper_futures_funding_defaults_to_one_basis_point() -> None:
    assert ExecutionSettings().paper_funding_bps == Decimal("1")
