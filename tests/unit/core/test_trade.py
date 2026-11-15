"""Trade: fee/funding sign conventions, notional, R multiple."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aegis_core import Side, Trade


def _trade(**overrides: object) -> Trade:
    payload: dict[str, object] = {
        "order_id": "ord_1",
        "symbol": "btc-usdt",
        "side": Side.BUY,
        "qty": Decimal("0.5"),
        "price": Decimal("30000"),
        "strategy_id": "simple_ema_cross",
    }
    payload.update(overrides)
    return Trade(**payload)


def test_defaults() -> None:
    trade = _trade()
    assert trade.trade_id.startswith("trd_")
    assert trade.symbol == "BTC/USDT"
    assert trade.fee == 0
    assert trade.funding == 0
    assert trade.r_multiple is None
    assert trade.filled_at.utcoffset() == timedelta(0)


def test_notional() -> None:
    assert _trade().notional == Decimal("15000")


def test_fee_is_a_cost_and_funding_is_signed() -> None:
    paying = _trade(fee=Decimal("7.5"), funding=Decimal("-1.25"))
    receiving = _trade(fee=Decimal("7.5"), funding=Decimal("2"))
    assert paying.fees_and_funding == Decimal("6.25")
    assert receiving.fees_and_funding == Decimal("9.5")


def test_negative_fee_is_rejected() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        _trade(fee=Decimal("-1"))


def test_signed_qty_follows_side() -> None:
    assert _trade().signed_qty == Decimal("0.5")
    assert _trade(side=Side.SELL).signed_qty == Decimal("-0.5")


def test_r_multiple_is_stored() -> None:
    assert _trade(r_multiple=Decimal("-1.5")).r_multiple == Decimal("-1.5")


def test_trade_requires_a_strategy_id() -> None:
    with pytest.raises(ValidationError, match="strategy_id"):
        _trade(strategy_id="")
