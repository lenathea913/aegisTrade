"""Dashboard number formatting: rounding at the edge, never in the ledger."""

from __future__ import annotations

from decimal import Decimal

from api.app import TEMPLATES
from api.formatting import clean_payload, money, price, qty


def test_money_keeps_two_decimals() -> None:
    assert money("9999.09955000000000010018009") == "9999.10"
    assert money(Decimal("10000")) == "10000.00"
    assert money("-12.005") == "-12.01"


def test_price_keeps_four_decimals() -> None:
    assert price("30015.0000000001") == "30015.0000"
    assert price(Decimal("0.5")) == "0.5000"


def test_qty_trims_trailing_zeros() -> None:
    assert qty("0.060000000000000001") == "0.06"
    assert qty(Decimal("0.0000004")) == "0"
    assert qty(Decimal("1")) == "1"


def test_non_numeric_values_pass_through() -> None:
    assert money(None) == ""
    assert money("n/a") == "n/a"
    assert qty("market") == "market"


def test_clean_payload_rounds_nested_numbers_only() -> None:
    cleaned = clean_payload(
        {
            "fee": "0.03065000000000000001",
            "price": "30015.000000000003",
            "nested": {"pnl": "9999.0995500000000001", "qty": "0.03"},
            "r_multiple": None,
            "strategy_id": "simple_ema_cross",
            "rows": ["1.234567891", "not-a-number"],
        }
    )

    assert cleaned["fee"] == "0.03065"
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
    assert filters["clean_payload"] is clean_payload
