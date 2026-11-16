"""Dashboard number formatting: rounding at the edge, never in the ledger."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest

from aegis_marketdata import InstrumentSpec
from api.app import TEMPLATES
from api.formatting import clean_payload, fee, money, price, qty
from api.instruments import REGISTRY

BTC = InstrumentSpec("BTC/USDT", Decimal("0.01"), Decimal("0.00001"))
SHIB = InstrumentSpec("SHIB/USDT", Decimal("0.000001"), Decimal("1"))


@pytest.fixture()
def primed() -> Iterator[None]:
    REGISTRY.replace({"BTC/USDT": BTC, "SHIB/USDT": SHIB})
    try:
        yield
    finally:
        REGISTRY.replace({})


def test_money_keeps_two_decimals() -> None:
    assert money("9999.09955000000000010018009") == "9999.10"
    assert money(Decimal("10000")) == "10000.00"
    assert money("-12.005") == "-12.01"


def test_price_uses_the_symbol_tick(primed: None) -> None:
    assert price("30015.0000000001", "BTC/USDT") == "30015.00"
    assert price("0.000012345", "SHIB/USDT") == "0.000012"
    # Unknown symbols fall back to two decimals instead of inventing precision.
    assert price(Decimal("0.5"), "DOGE/USDT") == "0.50"
    assert price(None, "BTC/USDT") == ""


def test_qty_uses_the_symbol_step_and_trims_zeros(primed: None) -> None:
    assert qty("0.060000000000000001", "BTC/USDT") == "0.06"
    assert qty(Decimal("12.5"), "SHIB/USDT") == "12"  # step size 1
    assert qty(Decimal("0.0000004"), "BTC/USDT") == "0"
    assert qty(Decimal("1"), "BTC/USDT") == "1"


def test_fee_never_rounds_to_zero() -> None:
    assert fee("0.0045022500000000002") == "0.00450225"
    assert fee("0.004502") == "0.004502"
    assert fee("0") == "0.000000"


def test_non_numeric_values_pass_through() -> None:
    assert money(None) == ""
    assert money("n/a") == "n/a"
    assert qty("market", "BTC/USDT") == "market"


def test_clean_payload_rounds_by_field(primed: None) -> None:
    cleaned = clean_payload(
        {
            "fee": "0.0045022500000000002",
            "price": "30015.000000000003",
            "nested": {"pnl": "9999.0995500000000001", "qty": "0.03"},
            "r_multiple": None,
            "strategy_id": "simple_ema_cross",
            "rows": ["1.234567891", "not-a-number"],
        }
    )

    assert cleaned["fee"] == "0.00450225"  # a fee keeps its precision
    assert cleaned["price"] == "30015.00"
    assert cleaned["nested"]["pnl"] == "9999.10"
    assert cleaned["nested"]["qty"] == "0.03"
    assert cleaned["r_multiple"] is None
    assert cleaned["strategy_id"] == "simple_ema_cross"
    assert cleaned["rows"] == ["1.23", "not-a-number"]


def test_the_templates_have_the_filters_registered() -> None:
    filters = TEMPLATES.env.filters

    assert filters["money"] is money
    assert filters["price"] is price
    assert filters["qty"] is qty
    assert filters["fee"] is fee
    assert filters["clean_payload"] is clean_payload
