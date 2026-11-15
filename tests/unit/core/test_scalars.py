"""Scalar/primitive behaviour: UTC handling, symbol normalisation, Decimal guards, ids."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from aegis_core import new_id, to_utc, utc_now
from aegis_core.types import (
    ExposurePercent,
    LeverageDecimal,
    NonNegativeDecimal,
    Percent,
    PositiveDecimal,
    SymbolStr,
    UtcDatetime,
    normalise_symbol,
)


class _SymbolBox(BaseModel):
    symbol: SymbolStr


class _PositiveBox(BaseModel):
    value: PositiveDecimal


class _NonNegativeBox(BaseModel):
    value: NonNegativeDecimal


class _PercentBox(BaseModel):
    value: Percent


class _LeverageBox(BaseModel):
    value: LeverageDecimal


class _ExposureBox(BaseModel):
    value: ExposurePercent


class _TimeBox(BaseModel):
    ts: UtcDatetime


# --------------------------------------------------------------------------- UTC


def test_utc_now_is_aware_and_utc() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_to_utc_converts_offsets() -> None:
    jakarta = timezone(timedelta(hours=7))
    local = datetime(2026, 1, 1, 12, 0, tzinfo=jakarta)
    assert to_utc(local) == datetime(2026, 1, 1, 5, 0, tzinfo=UTC)
    assert to_utc(local).tzinfo is UTC


def test_to_utc_rejects_naive() -> None:
    with pytest.raises(ValueError, match="naive"):
        to_utc(datetime(2026, 1, 1, 12, 0))


def test_model_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        _TimeBox(ts=datetime(2026, 1, 1, 12, 0))


def test_model_converts_non_utc_datetime() -> None:
    jakarta = timezone(timedelta(hours=7))
    box = _TimeBox(ts=datetime(2026, 1, 1, 12, 0, tzinfo=jakarta))
    assert box.ts == datetime(2026, 1, 1, 5, 0, tzinfo=UTC)


# ------------------------------------------------------------------------ symbols


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BTC/USDT", "BTC/USDT"),
        ("btc/usdt", "BTC/USDT"),
        ("  btc-usdt  ", "BTC/USDT"),
        ("ETHUSDT", "ETHUSDT"),
        ("1inch/usdt", "1INCH/USDT"),
    ],
)
def test_symbol_normalisation(raw: str, expected: str) -> None:
    assert normalise_symbol(raw) == expected


@pytest.mark.parametrize("raw", ["", "B", "BTC/USDT/PERP", "BTC$USDT", "-BTC"])
def test_symbol_rejects_garbage(raw: str) -> None:
    with pytest.raises(ValidationError):
        _SymbolBox(symbol=raw)


# ------------------------------------------------------------------------ decimals


def test_positive_decimal_accepts_string_input_and_keeps_precision() -> None:
    box = _PositiveBox(value="0.00000001")
    assert box.value == Decimal("0.00000001")
    assert isinstance(box.value, Decimal)


@pytest.mark.parametrize("raw", [0, "-1", "0.0"])
def test_positive_decimal_rejects_non_positive(raw: object) -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        _PositiveBox(value=raw)


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_decimal_aliases_reject_non_finite(raw: str) -> None:
    for box in (_PositiveBox, _NonNegativeBox, _PercentBox, _LeverageBox):
        with pytest.raises(ValidationError, match="finite"):
            box(value=raw)


def test_finite_guard_rejects_nan_and_infinity_directly() -> None:
    """The guard is also reachable outside pydantic parsing (services, ledger)."""
    from aegis_core.types import _finite  # internal guard, tested on purpose

    for raw in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError, match="finite"):
            _finite(Decimal(raw))
    assert _finite(Decimal("1.5")) == Decimal("1.5")


def test_non_negative_decimal_allows_zero() -> None:
    assert _NonNegativeBox(value=0).value == 0


@pytest.mark.parametrize("raw", ["-0.0001", "-5"])
def test_non_negative_decimal_rejects_negative(raw: str) -> None:
    with pytest.raises(ValidationError):
        _NonNegativeBox(value=raw)


# ---------------------------------------------------------------------- percentages


@pytest.mark.parametrize("raw", ["0", "-1", "100.01", "250"])
def test_percent_bounds(raw: str) -> None:
    with pytest.raises(ValidationError, match=r"\(0, 100\]"):
        _PercentBox(value=raw)


@pytest.mark.parametrize("raw", ["0.01", "1", "100"])
def test_percent_accepts_valid_range(raw: str) -> None:
    assert _PercentBox(value=raw).value == Decimal(raw)


# ------------------------------------------------------------------------ leverage


def test_exposure_percent_allows_futures_leverage() -> None:
    assert _ExposureBox(value="300").value == Decimal("300")
    assert _ExposureBox(value="100").value == Decimal("100")
    with pytest.raises(ValidationError, match=r"\(0, 1000\]"):
        _ExposureBox(value="1000.01")
    with pytest.raises(ValidationError, match=r"\(0, 1000\]"):
        _ExposureBox(value="0")


def test_leverage_must_be_at_least_one() -> None:
    with pytest.raises(ValidationError, match="leverage"):
        _LeverageBox(value="0.5")
    assert _LeverageBox(value="1").value == Decimal("1")


# ----------------------------------------------------------------------------- ids


def test_new_id_has_prefix_and_unique_body() -> None:
    first, second = new_id("ord"), new_id("ord")
    assert first.startswith("ord_")
    assert len(first) == len("ord_") + 32
    assert first != second


def test_new_id_rejects_unknown_prefix() -> None:
    with pytest.raises(ValueError, match="unknown id prefix"):
        new_id("nope")
