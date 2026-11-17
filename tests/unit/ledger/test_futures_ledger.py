"""Futures accounting in the ledger: margin, ROE and simulated funding.

PnL on a linear contract does not depend on leverage; the *margin* does. These tests
pin both, and the funding simulation that makes a multi-day paper position honest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aegis_core import Side, Trade
from aegis_ledger import PortfolioLedger

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def open_position(*, side: Side = Side.BUY, qty: str = "0.1", leverage: int = 5) -> PortfolioLedger:
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))
    ledger.apply_trade(
        Trade(
            order_id="ord_open",
            symbol="BTC/USDT",
            side=side,
            qty=Decimal(qty),
            price=Decimal("30000"),
            fee=Decimal("0"),
            filled_at=NOW,
            strategy_id="simple_ema_cross",
            leverage=leverage,
        )
    )
    return ledger


def test_the_position_keeps_the_leverage_it_was_opened_with() -> None:
    ledger = open_position(leverage=3)

    position = ledger.position("BTC/USDT")

    assert position.leverage == 3
    assert position.margin_used == Decimal("1000")  # 3 000 notional / 3


def test_unrealized_pnl_is_leverage_independent_but_roe_is_not() -> None:
    three = open_position(leverage=3)
    five = open_position(leverage=5)

    for ledger in (three, five):
        ledger.mark_price("BTC/USDT", Decimal("30300"))

    assert three.unrealized_pnl == five.unrealized_pnl == Decimal("30")
    assert three.position("BTC/USDT").roe_pct == Decimal("3")  # 30 / 1 000
    assert five.position("BTC/USDT").roe_pct == Decimal("5")  # 30 / 600


def test_a_short_profits_when_price_falls() -> None:
    ledger = open_position(side=Side.SELL, qty="0.1", leverage=5)

    ledger.mark_price("BTC/USDT", Decimal("29400"))

    position = ledger.position("BTC/USDT")
    assert ledger.unrealized_pnl == Decimal("60")
    assert position.roe_pct == Decimal("10")
    assert position.liquidation_price is not None
    assert position.liquidation_price > position.avg_entry


def test_closing_keeps_the_split_between_pnl_and_margin() -> None:
    ledger = open_position(leverage=4)
    ledger.apply_trade(
        Trade(
            order_id="ord_close",
            symbol="BTC/USDT",
            side=Side.SELL,
            qty=Decimal("0.1"),
            price=Decimal("30300"),
            fee=Decimal("1"),
            filled_at=NOW + timedelta(hours=2),
            strategy_id="position_monitor",
            leverage=4,
        )
    )

    position = ledger.position("BTC/USDT")
    assert position.is_flat
    assert position.leverage == 1  # flat posts no margin
    assert ledger.realized_pnl == Decimal("29")  # 30 profit - 1 fee
    assert ledger.margin_used == Decimal("0")


def test_funding_is_charged_once_per_interval() -> None:
    ledger = open_position(leverage=5)
    rate = Decimal("0.0001")  # 0.01 % per 8h

    charged = ledger.accrue_funding(NOW + timedelta(hours=8), rate=rate)
    early = ledger.accrue_funding(NOW + timedelta(hours=9), rate=rate)

    assert charged == Decimal("0.3")  # 3 000 notional * 0.0001
    assert early == Decimal("0")

    charged_again = ledger.accrue_funding(NOW + timedelta(hours=16), rate=rate)
    assert charged_again == Decimal("0.3")


def test_funding_is_a_credit_for_shorts() -> None:
    ledger = open_position(side=Side.SELL, leverage=5)

    paid = ledger.accrue_funding(NOW + timedelta(hours=8), rate=Decimal("0.0001"))

    assert paid == Decimal("-0.3")
    assert ledger.realized_pnl == Decimal("0.3")  # a short receives positive funding
    assert ledger.equity == Decimal("10000.3")


def test_funding_stops_when_the_position_closes() -> None:
    ledger = open_position(leverage=5)
    ledger.apply_trade(
        Trade(
            order_id="ord_close",
            symbol="BTC/USDT",
            side=Side.SELL,
            qty=Decimal("0.1"),
            price=Decimal("30000"),
            fee=Decimal("0"),
            filled_at=NOW + timedelta(hours=1),
            strategy_id="position_monitor",
            leverage=5,
        )
    )

    assert ledger.accrue_funding(NOW + timedelta(hours=24), rate=Decimal("0.0001")) == 0


def test_a_negative_rate_is_refused() -> None:
    ledger = open_position()

    with pytest.raises(Exception, match="funding rate"):
        ledger.accrue_funding(NOW + timedelta(hours=8), rate=Decimal("-0.0001"))
