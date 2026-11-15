"""RiskLimit configuration invariants and RiskDecision verdict rules."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from aegis_core import RiskDecision, RiskLimit


def test_default_limits_are_conservative() -> None:
    limits = RiskLimit()
    assert limits.max_risk_per_trade_pct == Decimal("0.5")
    assert limits.max_daily_loss_pct == Decimal("2")
    assert limits.max_weekly_loss_pct == Decimal("5")
    assert limits.max_exposure_per_symbol_pct == Decimal("20")
    assert limits.max_total_exposure_pct == Decimal("60")
    assert limits.max_leverage == 1


def test_empty_allow_list_denies_everything() -> None:
    limits = RiskLimit()
    assert limits.allowed_symbols == frozenset()
    assert limits.allows("BTC/USDT") is False


def test_allow_list_is_normalised_and_queried_case_insensitively() -> None:
    limits = RiskLimit(allowed_symbols=frozenset({"btc-usdt", "ETH/USDT"}))
    assert limits.allowed_symbols == frozenset({"BTC/USDT", "ETH/USDT"})
    assert limits.allows("btc/usdt") is True
    assert limits.allows("SOL/USDT") is False


@pytest.mark.parametrize(
    "field", ["max_risk_per_trade_pct", "max_daily_loss_pct", "max_weekly_loss_pct"]
)
@pytest.mark.parametrize("value", ["0", "101"])
def test_percentages_are_validated(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        RiskLimit(**{field: Decimal(value)})


def test_weekly_must_cover_daily() -> None:
    with pytest.raises(ValidationError, match="max_weekly_loss_pct must be >="):
        RiskLimit(max_daily_loss_pct=Decimal("10"), max_weekly_loss_pct=Decimal("5"))


def test_symbol_exposure_cannot_exceed_total_exposure() -> None:
    with pytest.raises(ValidationError, match="must be <= max_total_exposure_pct"):
        RiskLimit(max_exposure_per_symbol_pct=Decimal("80"), max_total_exposure_pct=Decimal("60"))


def test_exposure_caps_may_exceed_equity_for_futures() -> None:
    limits = RiskLimit(
        max_exposure_per_symbol_pct=Decimal("150"), max_total_exposure_pct=Decimal("300")
    )
    assert limits.max_total_exposure_pct == Decimal("300")


def test_exposure_caps_are_still_bounded() -> None:
    with pytest.raises(ValidationError, match="1000"):
        RiskLimit(
            max_total_exposure_pct=Decimal("1001"), max_exposure_per_symbol_pct=Decimal("100")
        )


def test_loss_and_risk_percentages_remain_bounded_at_100() -> None:
    with pytest.raises(ValidationError):
        RiskLimit(max_risk_per_trade_pct=Decimal("101"))


def test_leverage_must_be_at_least_one() -> None:
    with pytest.raises(ValidationError, match="leverage"):
        RiskLimit(max_leverage=Decimal("0.99"))


def test_limits_are_frozen() -> None:
    limits = RiskLimit()
    with pytest.raises(ValidationError):
        limits.max_leverage = Decimal("3")  # type: ignore[misc]


# ------------------------------------------------------------- RiskDecision


def test_approval_records_checks_and_amount() -> None:
    decision = RiskDecision.approve(Decimal("250"), ("symbol_allowed", "risk_per_trade"))
    assert decision.approved is True
    assert decision.is_approved is True
    assert decision.max_risk_amount == Decimal("250")
    assert decision.reasons == ("symbol_allowed", "risk_per_trade")


def test_rejection_requires_a_reason() -> None:
    with pytest.raises(ValidationError, match="at least one reason"):
        RiskDecision(approved=False)


def test_rejection_keeps_all_reasons() -> None:
    decision = RiskDecision.reject("kill_switch_active", "stale_market_data")
    assert decision.approved is False
    assert decision.reasons == ("kill_switch_active", "stale_market_data")
    assert decision.max_risk_amount == 0


def test_max_risk_amount_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        RiskDecision(approved=True, max_risk_amount=Decimal("-1"))
