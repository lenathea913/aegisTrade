"""Ledger arithmetic: positions, fees/funding, marks, equity and loss windows."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from aegis_core import Side, Trade
from aegis_ledger import LedgerError, PortfolioLedger, RealizedEvent

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)


def trade(
    side: Side,
    qty: str,
    price: str,
    *,
    fee: str = "0",
    funding: str = "0",
    at: datetime = NOW,
    symbol: str = "BTC/USDT",
) -> Trade:
    return Trade(
        order_id="ord-1",
        symbol=symbol,
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        funding=Decimal(funding),
        filled_at=at,
        strategy_id="simple_ema_cross",
    )


@pytest.fixture
def ledger() -> PortfolioLedger:
    return PortfolioLedger(initial_equity=Decimal("10000"))


def test_empty_ledger_reports_initial_equity(ledger: PortfolioLedger) -> None:
    state = ledger.snapshot(now=NOW)

    assert state.equity == Decimal("10000")
    assert state.positions == ()
    assert state.daily_realized_pnl == Decimal("0")
    assert ledger.position("btc-usdt").is_flat


def test_opening_a_long_pays_the_fee_immediately(ledger: PortfolioLedger) -> None:
    update = ledger.apply_trade(trade(Side.BUY, "0.5", "30000", fee="7.5"))

    assert update.position.qty == Decimal("0.5")
    assert update.position.avg_entry == Decimal("30000")
    assert update.realized_delta == Decimal("-7.5")
    assert ledger.equity == Decimal("9992.5")
    assert ledger.realized_pnl == Decimal("-7.5")


def test_marking_updates_unrealized_and_equity(ledger: PortfolioLedger) -> None:
    ledger.apply_trade(trade(Side.BUY, "0.5", "30000"))
    ledger.mark_price("BTC/USDT", Decimal("31000"))

    assert ledger.unrealized_pnl == Decimal("500")
    assert ledger.equity == Decimal("10500")
    assert ledger.mark("BTC/USDT") == Decimal("31000")
    assert ledger.snapshot(now=NOW).positions[0].unrealized_pnl == Decimal("500")


def test_partial_close_realizes_pnl_and_keeps_the_basis(ledger: PortfolioLedger) -> None:
    ledger.apply_trade(trade(Side.BUY, "0.5", "30000"))
    update = ledger.apply_trade(trade(Side.SELL, "0.2", "31000"))

    assert update.realized_delta == Decimal("200")
    assert update.position.qty == Decimal("0.3")
    assert update.position.avg_entry == Decimal("30000")
    # marks to 31000, so the remaining 0.3 carries +300 unrealized
    assert ledger.equity == Decimal("10500")


def test_flip_realizes_the_old_leg_and_opens_a_short(ledger: PortfolioLedger) -> None:
    ledger.apply_trade(trade(Side.BUY, "0.5", "30000"))
    update = ledger.apply_trade(trade(Side.SELL, "0.8", "31000"))

    assert update.realized_delta == Decimal("500")
    assert update.position.qty == Decimal("-0.3")
    assert update.position.avg_entry == Decimal("31000")
    assert update.position.side is Side.SELL
    assert ledger.equity == Decimal("10500")


def test_funding_is_signed(ledger: PortfolioLedger) -> None:
    paid = ledger.apply_trade(trade(Side.BUY, "0.5", "30000", fee="10", funding="2"))
    assert paid.realized_delta == Decimal("-8")

    credited = ledger.apply_trade(trade(Side.BUY, "0.1", "30000", fee="0", funding="5"))
    assert credited.realized_delta == Decimal("5")
    assert ledger.equity == Decimal("9997")


def test_daily_and_weekly_windows_come_from_events(ledger: PortfolioLedger) -> None:
    ledger.restore(
        [],
        [
            RealizedEvent(Decimal("-50"), datetime(2026, 6, 1, 10, tzinfo=UTC), "t1", "BTC/USDT"),
            RealizedEvent(Decimal("30"), datetime(2026, 6, 2, 9, tzinfo=UTC), "t2", "BTC/USDT"),
        ],
    )

    state = ledger.snapshot(now=NOW)

    assert state.daily_realized_pnl == Decimal("30")
    assert state.weekly_realized_pnl == Decimal("-20")  # 2026-06-01 is the Monday
    first_day_end = datetime(2026, 6, 1, 23, tzinfo=UTC)
    assert ledger.realized_between(datetime(2026, 6, 1, tzinfo=UTC), first_day_end) == Decimal(
        "-50"
    )
    assert ledger.realized_between(datetime(2026, 6, 2, tzinfo=UTC), NOW) == Decimal("30")


def test_duplicate_symbols_are_rejected_by_the_snapshot_contract() -> None:
    from aegis_core import PortfolioState, Position

    position = Position(symbol="BTC/USDT", qty=Decimal("1"), avg_entry=Decimal("1"))

    with pytest.raises(ValueError, match="duplicate positions"):
        PortfolioState(equity=Decimal("1"), positions=(position, position))


def test_equity_depletion_blocks_further_decisions(ledger: PortfolioLedger) -> None:
    ledger.apply_trade(trade(Side.BUY, "100", "1000"))
    ledger.mark_price("BTC/USDT", Decimal("100"))

    assert ledger.equity == Decimal("-80000")
    with pytest.raises(LedgerError, match="equity depleted"):
        ledger.snapshot(now=NOW)


def test_invalid_inputs_are_refused() -> None:
    with pytest.raises(LedgerError, match="initial_equity"):
        PortfolioLedger(initial_equity=Decimal("0"))

    ledger = PortfolioLedger(initial_equity=Decimal("100"))
    with pytest.raises(LedgerError, match="mark price"):
        ledger.mark_price("BTC/USDT", Decimal("0"))


def test_restore_and_reads(ledger: PortfolioLedger) -> None:
    from aegis_core import Position

    ledger.restore([Position(symbol="BTC/USDT", qty=Decimal("2"), avg_entry=Decimal("100"))])
    ledger.mark_prices({"btc-usdt": Decimal("110")})

    assert ledger.position("BTC/USDT").unrealized_pnl == Decimal("20")
    assert ledger.equity == Decimal("10020")
    assert len(ledger.positions) == 1
    assert ledger.realized_events == ()
