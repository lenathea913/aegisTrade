"""Enum helpers used by the ledger, gateway and strategies."""

from __future__ import annotations

import pytest

from aegis_core import OrderStatus, OrderType, Side


def test_side_opposite() -> None:
    assert Side.BUY.opposite is Side.SELL
    assert Side.SELL.opposite is Side.BUY


def test_side_sign() -> None:
    assert Side.BUY.sign == 1
    assert Side.SELL.sign == -1


def test_sides_serialise_in_uppercase() -> None:
    assert Side.BUY.value == "BUY"
    assert Side("buy".upper()) is Side.BUY
    assert OrderType.MARKET.value == "MARKET"


@pytest.mark.parametrize(
    "status", [OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED]
)
def test_terminal_statuses(status: OrderStatus) -> None:
    assert status.is_terminal is True


@pytest.mark.parametrize(
    "status", [OrderStatus.NEW, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED]
)
def test_non_terminal_statuses(status: OrderStatus) -> None:
    assert status.is_terminal is False
