"""ApprovedOrder: mandatory evidence, time ordering, delegated read access."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aegis_core import ApprovedOrder, OrderIntent, OrderType, Side


def test_approval_id_is_generated(make_intent: Callable[..., OrderIntent]) -> None:
    approved = ApprovedOrder(intent=make_intent(), max_risk_amount=Decimal("500"))
    assert approved.approval_id.startswith("appr_")


def test_risk_checks_are_recorded(make_intent: Callable[..., OrderIntent]) -> None:
    approved = ApprovedOrder(
        intent=make_intent(),
        max_risk_amount=Decimal("500"),
        risk_checks_passed=("symbol_allowed", "risk_per_trade", "stale_data"),
    )
    assert approved.risk_checks_passed == ("symbol_allowed", "risk_per_trade", "stale_data")


def test_risk_checks_default_to_empty(make_intent: Callable[..., OrderIntent]) -> None:
    approved = ApprovedOrder(intent=make_intent(), max_risk_amount=Decimal("500"))
    assert approved.risk_checks_passed == ()


def test_max_risk_amount_must_be_positive(make_intent: Callable[..., OrderIntent]) -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        ApprovedOrder(intent=make_intent(), max_risk_amount=Decimal("0"))


def test_approval_id_must_not_be_blank(make_intent: Callable[..., OrderIntent]) -> None:
    with pytest.raises(ValidationError):
        ApprovedOrder(intent=make_intent(), max_risk_amount=Decimal("500"), approval_id="  ")


def test_approved_at_cannot_precede_the_intent(make_intent: Callable[..., OrderIntent]) -> None:
    intent = make_intent(created_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC))
    with pytest.raises(ValidationError, match="approved_at"):
        ApprovedOrder(
            intent=intent,
            max_risk_amount=Decimal("500"),
            approved_at=datetime(2026, 5, 1, 11, 59, tzinfo=UTC),
        )


def test_approved_at_may_equal_the_intent_time(make_intent: Callable[..., OrderIntent]) -> None:
    created = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    approved = ApprovedOrder(
        intent=make_intent(created_at=created),
        max_risk_amount=Decimal("500"),
        approved_at=created,
    )
    assert approved.approved_at == created


def test_delegated_read_access(approved: ApprovedOrder) -> None:
    assert approved.symbol == "BTC/USDT"
    assert approved.side is Side.BUY
    assert approved.order_type is OrderType.LIMIT
    assert approved.qty == Decimal("0.5")
    assert approved.price == Decimal("30000")
    assert approved.stop_loss == Decimal("29000")
    assert approved.take_profit == Decimal("32000")
    assert approved.strategy_id == "simple_ema_cross"
    assert approved.created_at == approved.intent.created_at


def test_approval_is_frozen(approved: ApprovedOrder) -> None:
    with pytest.raises(ValidationError):
        approved.max_risk_amount = Decimal("10000")  # type: ignore[misc]


def test_roundtrip_preserves_nested_intent(approved: ApprovedOrder) -> None:
    restored = ApprovedOrder.model_validate_json(approved.model_dump_json())
    assert restored == approved
    assert restored.intent == approved.intent
