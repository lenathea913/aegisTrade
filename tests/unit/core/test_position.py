"""Position: cost basis, partial closes, flips, marks and invariants."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from aegis_core import Position, Side
from aegis_core.errors import DomainError


def test_flat_position_defaults() -> None:
    position = Position.flat("btc-usdt")
    assert position.symbol == "BTC/USDT"
    assert position.is_flat
    assert position.side is None
    assert position.notional == 0
    assert position.avg_entry == 0


def test_non_flat_position_requires_cost_basis() -> None:
    with pytest.raises(ValidationError, match="positive avg_entry"):
        Position(symbol="BTC/USDT", qty=Decimal("1"))


def test_flat_position_with_cost_basis_is_rejected() -> None:
    with pytest.raises(ValidationError, match="flat position"):
        Position(symbol="BTC/USDT", qty=Decimal("0"), avg_entry=Decimal("100"))


def test_opening_a_long() -> None:
    position = Position.flat("BTC/USDT").apply_fill(Decimal("1"), Decimal("100"))
    assert position.qty == Decimal("1")
    assert position.avg_entry == Decimal("100")
    assert position.side is Side.BUY
    assert position.notional == Decimal("100")
    assert position.realized_pnl == 0


def test_adding_to_a_long_averages_the_entry() -> None:
    position = (
        Position.flat("BTC/USDT")
        .apply_fill(Decimal("1"), Decimal("100"))
        .apply_fill(Decimal("3"), Decimal("200"))
    )
    assert position.qty == Decimal("4")
    assert position.avg_entry == Decimal("175")  # (1*100 + 3*200) / 4


def test_partial_close_realizes_pnl_and_keeps_basis() -> None:
    position = (
        Position.flat("BTC/USDT")
        .apply_fill(Decimal("2"), Decimal("100"))
        .apply_fill(Decimal("-1"), Decimal("120"))
    )
    assert position.qty == Decimal("1")
    assert position.avg_entry == Decimal("100")
    assert position.realized_pnl == Decimal("20")


def test_full_close_flattens_and_realizes() -> None:
    position = (
        Position.flat("BTC/USDT").apply_fill(Decimal("2"), Decimal("100")).close(Decimal("90"))
    )
    assert position.is_flat
    assert position.avg_entry == 0
    assert position.realized_pnl == Decimal("-20")


def test_loss_accumulates_in_realized_pnl() -> None:
    position = (
        Position.flat("BTC/USDT")
        .apply_fill(Decimal("2"), Decimal("100"))
        .apply_fill(Decimal("-1"), Decimal("90"))
        .apply_fill(Decimal("-1"), Decimal("110"))
    )
    assert position.is_flat
    assert position.realized_pnl == Decimal("0")  # -10 + 10


def test_flipping_long_to_short_realizes_and_rebases() -> None:
    position = (
        Position.flat("BTC/USDT")
        .apply_fill(Decimal("1"), Decimal("100"))
        .apply_fill(Decimal("-3"), Decimal("90"))
    )
    assert position.qty == Decimal("-2")
    assert position.side is Side.SELL
    assert position.avg_entry == Decimal("90")
    assert position.realized_pnl == Decimal("-10")


def test_short_position_unrealized_pnl() -> None:
    short = Position.flat("BTC/USDT").apply_fill(Decimal("-2"), Decimal("100"))
    marked = short.with_mark(Decimal("90"))
    assert marked.unrealized_pnl == Decimal("20")  # (90 - 100) * -2
    assert short.unrealized_pnl == 0  # original untouched


def test_mark_of_flat_position_is_zero() -> None:
    assert Position.flat("BTC/USDT").with_mark(Decimal("123")).unrealized_pnl == 0


@pytest.mark.parametrize("price", ["0", "-1"])
def test_mark_price_must_be_positive(price: str) -> None:
    position = Position.flat("BTC/USDT").apply_fill(Decimal("1"), Decimal("100"))
    with pytest.raises(DomainError):
        position.with_mark(Decimal(price))


def test_zero_fill_is_rejected() -> None:
    with pytest.raises(DomainError, match="must not be zero"):
        Position.flat("BTC/USDT").apply_fill(Decimal("0"), Decimal("100"))


def test_negative_fill_price_is_rejected() -> None:
    with pytest.raises(DomainError, match="fill price"):
        Position.flat("BTC/USDT").apply_fill(Decimal("1"), Decimal("-100"))


def test_closing_a_flat_position_is_rejected() -> None:
    with pytest.raises(DomainError, match="already flat"):
        Position.flat("BTC/USDT").close(Decimal("100"))


def test_position_is_immutable() -> None:
    position = Position.flat("BTC/USDT").apply_fill(Decimal("1"), Decimal("100"))
    with pytest.raises(ValidationError):
        position.qty = Decimal("5")  # type: ignore[misc]
