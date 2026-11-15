"""Order: creation gate, partial fills, weighted average price, invalid transitions."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aegis_core import ApprovedOrder, Order, OrderIntent, OrderStatus, OrderType
from aegis_core.errors import DomainError, InvalidStateTransitionError


@pytest.fixture
def order(approved: ApprovedOrder) -> Order:
    return Order.from_approved(approved, broker="paper")


def test_order_is_created_only_from_an_approval(order: Order, approved: ApprovedOrder) -> None:
    assert order.approval_id == approved.approval_id
    assert order.status is OrderStatus.NEW
    assert order.broker == "paper"
    assert order.order_id.startswith("ord_")
    assert order.filled_qty == 0
    assert order.avg_fill_price is None


def test_mark_submitted_records_broker_id(order: Order) -> None:
    submitted = order.mark_submitted("ex-123")
    assert submitted.status is OrderStatus.SUBMITTED
    assert submitted.exchange_order_id == "ex-123"
    assert submitted.updated_at >= order.updated_at


def test_mark_submitted_twice_is_invalid(order: Order) -> None:
    submitted = order.mark_submitted("ex-123")
    with pytest.raises(InvalidStateTransitionError):
        submitted.mark_submitted("ex-456")


def test_mark_submitted_rejects_blank_exchange_id(order: Order) -> None:
    with pytest.raises(DomainError, match="exchange_order_id"):
        order.mark_submitted("   ")


def test_partial_fill_then_full_fill_computes_weighted_average(order: Order) -> None:
    partial = order.apply_fill(Decimal("0.2"), Decimal("30000"))
    assert partial.status is OrderStatus.PARTIALLY_FILLED
    assert partial.filled_qty == Decimal("0.2")
    assert partial.avg_fill_price == Decimal("30000")
    assert partial.remaining_qty == Decimal("0.3")

    full = partial.apply_fill(Decimal("0.3"), Decimal("31000"))
    assert full.status is OrderStatus.FILLED
    assert full.filled_qty == Decimal("0.5")
    assert full.avg_fill_price == Decimal("30600")  # (0.2*30000 + 0.3*31000) / 0.5
    assert full.remaining_qty == 0
    assert full.filled_notional == Decimal("15300")


def test_overfill_is_rejected(order: Order) -> None:
    with pytest.raises(DomainError, match="exceeds remaining"):
        order.apply_fill(Decimal("0.6"), Decimal("30000"))


@pytest.mark.parametrize(("qty", "price"), [("0", "30000"), ("0.1", "0"), ("-0.1", "30000")])
def test_non_positive_fill_is_rejected(order: Order, qty: str, price: str) -> None:
    with pytest.raises(DomainError):
        order.apply_fill(Decimal(qty), Decimal(price))


def test_filling_a_finished_order_is_rejected(order: Order) -> None:
    filled = order.apply_fill(Decimal("0.5"), Decimal("30000"))
    with pytest.raises(InvalidStateTransitionError):
        filled.apply_fill(Decimal("0.1"), Decimal("30000"))


def test_partial_fill_can_be_canceled(order: Order) -> None:
    partial = order.apply_fill(Decimal("0.2"), Decimal("30000"))
    canceled = partial.cancel()
    assert canceled.status is OrderStatus.CANCELED
    assert canceled.filled_qty == Decimal("0.2")
    assert canceled.is_terminal is True


def test_cancel_is_terminal(order: Order) -> None:
    canceled = order.cancel()
    with pytest.raises(InvalidStateTransitionError):
        canceled.cancel()


def test_expire_and_reject(order: Order) -> None:
    assert order.expire().status is OrderStatus.EXPIRED
    assert order.reject().status is OrderStatus.REJECTED


def test_expire_after_fill_is_invalid(order: Order) -> None:
    with pytest.raises(InvalidStateTransitionError):
        order.apply_fill(Decimal("0.5"), Decimal("30000")).expire()


def test_constructor_rejects_overfilled_quantity() -> None:
    with pytest.raises(ValidationError, match="cannot exceed qty"):
        Order(
            approval_id="appr_x",
            symbol="BTC/USDT",
            side="BUY",
            order_type=OrderType.MARKET,
            qty=Decimal("1"),
            broker="paper",
            filled_qty=Decimal("2"),
            avg_fill_price=Decimal("30000"),
        )


def test_reject_after_cancel_is_invalid(order: Order) -> None:
    with pytest.raises(InvalidStateTransitionError):
        order.cancel().reject()


def test_reject_after_submission_is_invalid(order: Order) -> None:
    with pytest.raises(InvalidStateTransitionError):
        order.mark_submitted("ex-1").reject()


def test_filled_requires_quantity_to_match() -> None:
    with pytest.raises(ValidationError, match="fully filled"):
        Order(
            approval_id="appr_x",
            symbol="BTC/USDT",
            side="BUY",
            order_type=OrderType.MARKET,
            qty=Decimal("1"),
            status=OrderStatus.FILLED,
            broker="paper",
            filled_qty=Decimal("0.5"),
            avg_fill_price=Decimal("30000"),
        )


def test_avg_price_without_fills_is_rejected() -> None:
    with pytest.raises(ValidationError, match="avg_fill_price must be None"):
        Order(
            approval_id="appr_x",
            symbol="BTC/USDT",
            side="BUY",
            order_type=OrderType.MARKET,
            qty=Decimal("1"),
            broker="paper",
            avg_fill_price=Decimal("30000"),
        )


def test_filled_qty_requires_average_price() -> None:
    with pytest.raises(ValidationError, match="avg_fill_price is required"):
        Order(
            approval_id="appr_x",
            symbol="BTC/USDT",
            side="BUY",
            order_type=OrderType.MARKET,
            qty=Decimal("1"),
            broker="paper",
            filled_qty=Decimal("0.5"),
        )


def test_limit_order_requires_price() -> None:
    with pytest.raises(ValidationError, match="LIMIT order requires a price"):
        Order(
            approval_id="appr_x",
            symbol="BTC/USDT",
            side="BUY",
            order_type=OrderType.LIMIT,
            qty=Decimal("1"),
            broker="paper",
        )


def test_market_order_needs_no_price() -> None:
    market = Order(
        approval_id="appr_x",
        symbol="BTC/USDT",
        side="BUY",
        order_type=OrderType.MARKET,
        qty=Decimal("1"),
        broker="paper",
    )
    assert market.price is None
    assert market.filled_notional == 0


def test_market_order_intent_can_be_ordered(
    make_intent: Callable[..., OrderIntent],
) -> None:
    intent = make_intent(order_type=OrderType.MARKET, price=None, take_profit=None)
    approved = ApprovedOrder(intent=intent, max_risk_amount=Decimal("500"))
    order = Order.from_approved(approved, broker="paper")
    assert order.price is None
    assert order.qty == intent.qty
