"""Signal persistence: mapping and the idempotent bar-keyed upsert."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aegis_core import Signal, SignalDirection, SignalStatus, Timeframe
from aegis_db import SignalRecord
from signal_service.store import signal_from_row, signal_row

BAR_CLOSE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def make_signal(**overrides: object) -> Signal:
    values: dict[str, object] = {
        "symbol": "BTC/USDT",
        "timeframe": Timeframe.H1,
        "strategy_id": "simple_ema_cross",
        "strategy_version": "1",
        "direction": SignalDirection.LONG,
        "entry_ref": Decimal("30000"),
        "stop_loss": Decimal("29500"),
        "take_profit": Decimal("31000"),
        "risk_reward": Decimal("2"),
        "reason_codes": ("ema_cross", "long_entry"),
        "market_context": {
            "bars": 120,
            "quote_as_of": (BAR_CLOSE - timedelta(hours=1)).isoformat(),
        },
        "created_at": BAR_CLOSE,
        "expires_at": BAR_CLOSE + timedelta(hours=1),
        "status": SignalStatus.RISK_OK,
        "status_reasons": ("ok",),
    }
    values.update(overrides)
    return Signal(**values)


def test_row_uses_the_closed_bar_not_the_scan_time() -> None:
    """Two scans of the same bar must collide on the unique key, not create rows."""
    first = make_signal(created_at=BAR_CLOSE)
    second = make_signal(created_at=BAR_CLOSE + timedelta(minutes=5))

    assert signal_row(first)["bar_close_time"] == BAR_CLOSE - timedelta(hours=1)
    assert signal_row(second)["bar_close_time"] == signal_row(first)["bar_close_time"]


def test_row_falls_back_to_created_at_without_a_context_stamp() -> None:
    bare = make_signal(market_context={})

    assert signal_row(bare)["bar_close_time"] == bare.created_at


def test_signal_survives_a_round_trip_through_a_row() -> None:
    signal = make_signal()
    row = SignalRecord(**signal_row(signal))

    restored = signal_from_row(row)

    assert restored == signal
    assert restored.market_context["quote_as_of"] == signal.market_context["quote_as_of"]
    assert restored.status is SignalStatus.RISK_OK
