"""OrderIntent: validation, price/stop geometry, risk helpers, serialisation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aegis_core import OrderIntent, OrderType, Side


def test_creates_with_defaults_and_utc_timestamp(make_intent: Callable[..., OrderIntent]) -> None:
    intent = make_intent()
    assert intent.created_at.tzinfo is not None
    assert intent.created_at.utcoffset() == timedelta(0)
    assert intent.symbol == "BTC/USDT"
    assert intent.side is Side.BUY
    assert intent.order_type is OrderType.LIMIT
    assert isinstance(intent.qty, Decimal)


def test_symbol_is_normalised(make_intent: Callable[..., OrderIntent]) -> None:
    assert make_intent(symbol=" btc-usdt ").symbol == "BTC/USDT"


def test_domain_objects_are_frozen(make_intent: Callable[..., OrderIntent]) -> None:
    assert OrderIntent.model_config["frozen"] is True
    assert OrderIntent.model_config["extra"] == "forbid"


def test_unknown_field_is_rejected(make_intent: Callable[..., OrderIntent]) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        make_intent(leverage=Decimal("3"))


@pytest.mark.parametrize("qty", ["0", "-1", "-0.01"])
def test_qty_must_be_positive(make_intent: Callable[..., OrderIntent], qty: str) -> None:
    with pytest.raises(ValidationError):
        make_intent(qty=Decimal(qty))


def test_stop_loss_is_mandatory(make_intent: Callable[..., OrderIntent]) -> None:
    with pytest.raises(ValidationError, match="stop_loss"):
        make_intent(stop_loss=None)


@pytest.mark.parametrize("reason", ["", "   "])
def test_reason_must_not_be_blank(make_intent: Callable[..., OrderIntent], reason: str) -> None:
    with pytest.raises(ValidationError):
        make_intent(reason=reason)


def test_reason_is_trimmed(make_intent: Callable[..., OrderIntent]) -> None:
    assert make_intent(reason="  breakout  ").reason == "breakout"


def test_strategy_id_rejects_spaces(make_intent: Callable[..., OrderIntent]) -> None:
    with pytest.raises(ValidationError):
        make_intent(strategy_id="simple ema cross")


# ----------------------------------------------------------------- geometry


def test_limit_order_requires_price(make_intent: Callable[..., OrderIntent]) -> None:
    with pytest.raises(ValidationError, match="LIMIT order requires a price"):
        make_intent(price=None)


def test_market_order_may_omit_price(make_intent: Callable[..., OrderIntent]) -> None:
    intent = make_intent(
        order_type=OrderType.MARKET, price=None, stop_loss=Decimal("29000"), take_profit=None
    )
    assert intent.price is None
    assert intent.risk_per_unit is None
    assert intent.risk_amount() is None
    assert intent.notional() is None


def test_buy_stop_must_be_below_entry(make_intent: Callable[..., OrderIntent]) -> None:
    with pytest.raises(ValidationError, match="BUY stop_loss must be below"):
        make_intent(stop_loss=Decimal("30500"))


def test_buy_stop_equal_to_entry_is_rejected(make_intent: Callable[..., OrderIntent]) -> None:
    with pytest.raises(ValidationError, match="differ"):
        make_intent(stop_loss=Decimal("30000"))


def test_buy_take_profit_must_be_above_entry(make_intent: Callable[..., OrderIntent]) -> None:
    with pytest.raises(ValidationError, match="BUY take_profit must be above"):
        make_intent(take_profit=Decimal("29500"))


def test_sell_stop_must_be_above_entry(make_intent: Callable[..., OrderIntent]) -> None:
    intent = make_intent(side=Side.SELL, stop_loss=Decimal("31000"), take_profit=Decimal("28000"))
    assert intent.side is Side.SELL
    assert intent.stop_loss == Decimal("31000")


def test_sell_with_stop_below_entry_is_rejected(make_intent: Callable[..., OrderIntent]) -> None:
    with pytest.raises(ValidationError, match="SELL stop_loss must be above"):
        make_intent(side=Side.SELL, stop_loss=Decimal("29000"))


def test_sell_take_profit_must_be_below_entry(make_intent: Callable[..., OrderIntent]) -> None:
    with pytest.raises(ValidationError, match="SELL take_profit must be below"):
        make_intent(side=Side.SELL, stop_loss=Decimal("31000"), take_profit=Decimal("33000"))


def test_take_profit_is_optional(make_intent: Callable[..., OrderIntent]) -> None:
    assert make_intent(take_profit=None).take_profit is None


# ------------------------------------------------------------------- risk helpers


def test_risk_per_unit_and_amount(make_intent: Callable[..., OrderIntent]) -> None:
    intent = make_intent()
    assert intent.risk_per_unit == Decimal("1000")
    assert intent.risk_amount() == Decimal("500")  # 1000 * 0.5
    assert intent.risk_amount(Decimal("29500")) == Decimal("250")


def test_notional_uses_given_price(make_intent: Callable[..., OrderIntent]) -> None:
    intent = make_intent()
    assert intent.notional() == Decimal("15000")
    assert intent.notional(Decimal("31000")) == Decimal("15500")


def test_created_at_is_converted_to_utc(make_intent: Callable[..., OrderIntent]) -> None:
    jakarta = timezone(timedelta(hours=7))
    intent = make_intent(created_at=datetime(2026, 3, 1, 12, 0, tzinfo=jakarta))
    assert intent.created_at == datetime(2026, 3, 1, 5, 0, tzinfo=UTC)


def test_naive_created_at_is_rejected(make_intent: Callable[..., OrderIntent]) -> None:
    with pytest.raises(ValidationError):
        make_intent(created_at=datetime(2026, 3, 1, 12, 0))


def test_json_roundtrip_keeps_decimals_and_timezone(
    make_intent: Callable[..., OrderIntent],
) -> None:
    intent = make_intent()
    restored = OrderIntent.model_validate_json(intent.model_dump_json())
    assert restored == intent
    assert isinstance(restored.qty, Decimal)
    assert restored.created_at.utcoffset() == timedelta(0)


def test_instances_are_hashable_for_audit_grouping(
    make_intent: Callable[..., OrderIntent],
) -> None:
    assert {make_intent(), make_intent()}  # two distinct, hashable intents
