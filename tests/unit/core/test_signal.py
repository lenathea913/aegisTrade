"""Signal domain: immutable ideas with levels and a hard expiry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aegis_core import Signal, SignalDirection, SignalStatus, Timeframe

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def make_signal(**overrides: object) -> Signal:
    values: dict[str, object] = {
        "symbol": "BTC/USDT",
        "timeframe": Timeframe.H1,
        "strategy_id": "simple_ema_cross",
        "strategy_version": "1",
        "direction": SignalDirection.LONG,
        "entry_ref": Decimal("30000"),
        "stop_loss": Decimal("29400"),
        "take_profit": Decimal("31200"),
        "risk_reward": Decimal("2"),
        "reason_codes": ("ema_cross",),
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(overrides)
    return Signal(**values)


def test_signal_is_immutable_and_starts_new() -> None:
    signal = make_signal()

    assert signal.status is SignalStatus.NEW
    assert signal.is_expired(NOW + timedelta(minutes=59)) is False
    assert signal.is_expired(NOW + timedelta(hours=1)) is True
    with pytest.raises(ValidationError):
        signal.status = SignalStatus.RISK_OK  # type: ignore[misc]


def test_signals_must_expire_after_they_are_created() -> None:
    with pytest.raises(ValidationError):
        make_signal(expires_at=NOW)


def test_long_levels_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="LONG signal"):
        make_signal(stop_loss=Decimal("30500"))


def test_short_levels_must_be_mirrored() -> None:
    signal = make_signal(
        direction=SignalDirection.SHORT,
        entry_ref=Decimal("30000"),
        stop_loss=Decimal("30600"),
        take_profit=Decimal("28800"),
    )

    assert signal.direction.side.value == "SELL"
    with pytest.raises(ValidationError, match="SHORT signal"):
        make_signal(
            direction=SignalDirection.SHORT,
            entry_ref=Decimal("30000"),
            stop_loss=Decimal("29400"),
            take_profit=Decimal("31200"),
        )


def test_risk_reward_is_computed_from_the_levels() -> None:
    assert Signal.risk_reward_for(Decimal("30000"), Decimal("29400"), Decimal("31200")) == Decimal(
        "2"
    )


def test_status_changes_return_a_new_signal() -> None:
    signal = make_signal()

    updated = signal.with_status(SignalStatus.RISK_OK)

    assert updated.status is SignalStatus.RISK_OK
    assert signal.status is SignalStatus.NEW
    assert updated.signal_id == signal.signal_id
    assert updated.is_actionable is True


def test_blocked_signal_is_not_actionable() -> None:
    signal = make_signal().with_status(
        SignalStatus.BLOCKED_BY_RISK, ("max_exposure_per_symbol_exceeded",)
    )

    assert signal.is_actionable is False
    assert signal.status_reasons == ("max_exposure_per_symbol_exceeded",)
