"""A restart must not hand the bot a fresh loss budget.

Before this test the daemon rebuilt positions but the loss windows were whatever the
in-memory ledger had accumulated since boot. Restarting after a bad day therefore reset
the daily limit to zero and let the runner open risk it had already spent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from aegis_config import load_settings
from aegis_core import MarketContext, MarketQuote, OrderIntent, OrderType, Side, Timeframe
from aegis_ledger import PortfolioLedger
from aegis_risk import RiskGate
from aegis_risk.reasons import RejectReason

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)


def trade_row(
    *,
    trade_id: str,
    side: str,
    qty: str,
    price: str,
    fee: str = "0",
    at: datetime = NOW,
) -> SimpleNamespace:
    return SimpleNamespace(
        trade_id=trade_id,
        order_id="ord-1",
        symbol="BTC/USDT",
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        funding=Decimal("0"),
        filled_at=at,
        strategy_id="simple_ema_cross",
        r_multiple=None,
    )


def position_row(*, qty: str, avg_entry: str, realized: str) -> SimpleNamespace:
    return SimpleNamespace(
        symbol="BTC/USDT",
        qty=Decimal(qty),
        avg_entry=Decimal(avg_entry),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal(realized),
        opened_at=NOW,
    )


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> list[Any]:
        return self._rows


class FakeSession:
    """Routes each ``execute`` to the right table by inspecting the statement."""

    def __init__(self, trades: list[Any], positions: list[Any]) -> None:
        self._trades = trades
        self._positions = positions

    def execute(self, statement: Any) -> _Result:
        sql = str(statement)
        return _Result(self._positions if "trading.positions" in sql else self._trades)


def entry_intent() -> OrderIntent:
    return OrderIntent(
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        qty=Decimal("0.01"),
        price=Decimal("29000"),
        stop_loss=Decimal("28500"),
        take_profit=Decimal("30000"),
        strategy_id="simple_ema_cross",
        reason="restart test entry",
        created_at=NOW,
    )


def test_a_restart_replays_todays_loss_and_blocks_new_entries() -> None:
    # 1 BTC bought at 30 000 and sold at 29 800: exactly -200 on 10 000 equity, i.e. the
    # 2 % daily limit. Both fills happened today.
    trades = [
        trade_row(trade_id="t1", side="BUY", qty="1", price="30000", fee="0"),
        trade_row(
            trade_id="t2",
            side="SELL",
            qty="1",
            price="29800",
            fee="0",
            at=NOW + timedelta(minutes=30),
        ),
    ]
    session = FakeSession(trades, [position_row(qty="0", avg_entry="0", realized="-200")])

    restarted_at = NOW + timedelta(minutes=45)
    restarted = PortfolioLedger(initial_equity=Decimal("10000"))
    summary = restarted.reconstruct_from_db(session, now=restarted_at)

    # The replay is what a restart gets, not a ledger that starts at zero.
    assert summary.daily_realized_pnl == Decimal("-200")
    assert summary.weekly_realized_pnl == Decimal("-200")
    assert restarted.snapshot(now=restarted_at).daily_realized_pnl == Decimal("-200")

    settings = load_settings("paper")
    gate = RiskGate.from_settings(settings, Timeframe.H1)
    market = MarketContext(
        quotes=(MarketQuote(symbol="BTC/USDT", price=Decimal("29000"), as_of=restarted_at),),
        now=restarted_at,
    )

    decision = gate.evaluate(entry_intent(), restarted.snapshot(now=restarted_at), market)

    assert decision.approved is False
    assert any(
        reason.startswith(RejectReason.DAILY_LOSS_LIMIT_REACHED.value)
        for reason in decision.reasons
    )


def test_yesterdays_loss_does_not_consume_todays_budget() -> None:
    """The replay must respect the window, not reject everything it finds in the table."""
    trades = [
        trade_row(
            trade_id="t1",
            side="BUY",
            qty="1",
            price="30000",
            at=NOW - timedelta(days=9),
        ),
        trade_row(
            trade_id="t2",
            side="SELL",
            qty="1",
            price="29800",
            at=NOW - timedelta(days=8),
        ),
    ]
    session = FakeSession(trades, [position_row(qty="0", avg_entry="0", realized="-200")])
    ledger = PortfolioLedger(initial_equity=Decimal("10000"))

    summary = ledger.reconstruct_from_db(session, now=NOW)

    assert summary.equity == Decimal("9800")
    assert summary.daily_realized_pnl == Decimal("0")
    assert summary.weekly_realized_pnl == Decimal("0")
