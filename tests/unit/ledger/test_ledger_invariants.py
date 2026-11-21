"""Accounting invariants that must hold after every single fill.

Unit tests assert expected numbers; these assert the *identities* behind them, on a
sequence that adds to a position, partially closes it, flips through zero and holds two
symbols at once. If the ledger ever grows a second way to compute money, one of these
fails immediately.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aegis_core import Side, Trade
from aegis_ledger import PortfolioLedger

INITIAL = Decimal("10000")
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


def assert_invariants(ledger: PortfolioLedger, expected_qty: dict[str, Decimal]) -> None:
    """The two identities the rest of the system is allowed to rely on."""
    assert ledger.equity == ledger.initial_equity + ledger.realized_pnl + ledger.unrealized_pnl
    for symbol, qty in expected_qty.items():
        assert ledger.position(symbol).qty == qty, symbol


def test_equity_and_quantities_hold_after_every_fill() -> None:
    ledger = PortfolioLedger(initial_equity=INITIAL)
    fills = [
        # accumulate
        trade(Side.BUY, "0.5", "30000", fee="7.5"),
        trade(Side.BUY, "0.25", "31000", fee="3.875", at=NOW + timedelta(minutes=1)),
        # partial close
        trade(Side.SELL, "0.5", "32000", fee="8", at=NOW + timedelta(minutes=2)),
        # flip through zero into a short
        trade(Side.SELL, "1", "29000", fee="14.5", at=NOW + timedelta(minutes=3)),
        # close the short
        trade(Side.BUY, "0.75", "28500", fee="5.34", at=NOW + timedelta(minutes=4)),
        # a second symbol, so the sums cannot accidentally work per-ledger
        trade(Side.BUY, "2", "2000", fee="2", at=NOW + timedelta(minutes=5), symbol="ETH/USDT"),
    ]

    expected: dict[str, Decimal] = {"BTC/USDT": Decimal("0"), "ETH/USDT": Decimal("0")}
    for fill in fills:
        ledger.apply_trade(fill)
        expected[fill.symbol] = expected[fill.symbol] + fill.signed_qty
        assert_invariants(ledger, expected)

    assert ledger.position("BTC/USDT").is_flat
    assert ledger.position("ETH/USDT").qty == Decimal("2")


def test_the_identities_survive_marks_and_funding() -> None:
    ledger = PortfolioLedger(initial_equity=INITIAL)
    ledger.apply_trade(trade(Side.BUY, "0.5", "30000", fee="7.5", at=NOW - timedelta(hours=9)))

    # Funding is realized cost; a mark is unrealized. Both move equity, neither may
    # break the identity between the three components.
    ledger.accrue_funding(NOW, rate=Decimal("0.0001"))
    assert_invariants(ledger, {"BTC/USDT": Decimal("0.5")})

    ledger.mark_prices({"BTC/USDT": Decimal("31500")})
    assert_invariants(ledger, {"BTC/USDT": Decimal("0.5")})
    assert ledger.unrealized_pnl > 0

    ledger.mark_prices({"BTC/USDT": Decimal("28000")})
    assert_invariants(ledger, {"BTC/USDT": Decimal("0.5")})
    assert ledger.unrealized_pnl < 0


def test_realized_pnl_is_exactly_the_sum_of_its_events() -> None:
    ledger = PortfolioLedger(initial_equity=INITIAL)
    ledger.apply_trade(trade(Side.BUY, "1", "30000", fee="7.5"))
    ledger.apply_trade(trade(Side.SELL, "1", "30500", fee="7.625", at=NOW + timedelta(minutes=1)))
    ledger.accrue_funding(NOW + timedelta(hours=9), rate=Decimal("0.0001"))
    ledger.apply_trade(trade(Side.BUY, "1", "30000", fee="7.5", at=NOW + timedelta(hours=10)))

    assert ledger.realized_pnl == sum(
        (event.amount for event in ledger.realized_events), Decimal("0")
    )
