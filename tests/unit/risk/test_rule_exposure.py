"""Rules 4 and 5: total and per-symbol exposure.

Exposure is marked at the latest quote and falls back to cost basis when a symbol
has no quote. Caps may be touched (``>`` rejects, ``==`` passes), and because a
per-symbol cap can never exceed the total cap, a total-only rejection needs
exposure in *another* symbol.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from aegis_core import (
    MarketContext,
    MarketQuote,
    OrderIntent,
    PortfolioState,
    Position,
    RiskLimit,
)
from aegis_risk import RejectReason, RiskCheck, evaluate


def exposure_limits(total_pct: str, symbol_pct: str) -> RiskLimit:
    return RiskLimit(
        max_risk_per_trade_pct=Decimal("2"),
        max_daily_loss_pct=Decimal("5"),
        max_weekly_loss_pct=Decimal("10"),
        max_exposure_per_symbol_pct=Decimal(symbol_pct),
        max_total_exposure_pct=Decimal(total_pct),
        max_leverage=Decimal("1"),
        allowed_symbols=frozenset({"BTC/USDT"}),
    )


def failure_codes(reasons: tuple[str, ...]) -> set[str]:
    return {reason.split(":")[0] for reason in reasons}


def test_intent_within_total_and_symbol_caps(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    decision = evaluate(small_intent, portfolio, exposure_limits("100", "50"), market)

    assert decision.approved is True
    assert RiskCheck.TOTAL_EXPOSURE_WITHIN_LIMIT.value in decision.reasons
    assert RiskCheck.SYMBOL_EXPOSURE_WITHIN_LIMIT.value in decision.reasons


def test_total_exposure_can_fail_while_symbol_cap_holds(
    small_intent: OrderIntent,
    market: MarketContext,
) -> None:
    # 2 000 of ETH (no quote -> marked at cost) + 3 000 BTC = 5 000 > 40 % (4 000),
    # while BTC alone is exactly 30 % (3 000) and therefore still within its cap.
    held = PortfolioState(
        equity=Decimal("10000"),
        positions=(Position(symbol="ETH/USDT", qty=Decimal("1"), avg_entry=Decimal("2000")),),
    )

    decision = evaluate(small_intent, held, exposure_limits("40", "30"), market)

    assert decision.approved is False
    assert failure_codes(decision.reasons) == {RejectReason.MAX_TOTAL_EXPOSURE_EXCEEDED.value}


def test_total_exposure_above_cap_is_rejected(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    decision = evaluate(small_intent, portfolio, exposure_limits("20", "20"), market)

    assert decision.approved is False
    assert RejectReason.MAX_TOTAL_EXPOSURE_EXCEEDED.value in failure_codes(decision.reasons)


def test_existing_position_counts_towards_total_exposure(
    small_intent: OrderIntent,
    market: MarketContext,
) -> None:
    held = PortfolioState(
        equity=Decimal("10000"),
        positions=(Position(symbol="BTC/USDT", qty=Decimal("0.2"), avg_entry=Decimal("30000")),),
    )

    decision = evaluate(small_intent, held, exposure_limits("80", "80"), market)

    # 6 000 held + 3 000 new = 9 000 > 8 000
    assert decision.approved is False
    assert RejectReason.MAX_TOTAL_EXPOSURE_EXCEEDED.value in failure_codes(decision.reasons)


def test_per_symbol_cap_is_rejected_independently_of_total(
    small_intent: OrderIntent,
    market: MarketContext,
) -> None:
    held = PortfolioState(
        equity=Decimal("10000"),
        positions=(Position(symbol="BTC/USDT", qty=Decimal("0.2"), avg_entry=Decimal("30000")),),
    )

    decision = evaluate(small_intent, held, exposure_limits("100", "50"), market)

    # 9 000 > 5 000 per-symbol, but within the 10 000 total cap.
    assert decision.approved is False
    assert failure_codes(decision.reasons) == {RejectReason.MAX_SYMBOL_EXPOSURE_EXCEEDED.value}


def test_exposure_uses_market_marks_not_cost_basis(
    small_intent: OrderIntent,
    market: MarketContext,
) -> None:
    # Bought at 10 000, now marked at 30 000: 6 000 of exposure, not 2 000.
    held = PortfolioState(
        equity=Decimal("10000"),
        positions=(Position(symbol="BTC/USDT", qty=Decimal("0.2"), avg_entry=Decimal("10000")),),
    )

    decision = evaluate(small_intent, held, exposure_limits("100", "50"), market)

    assert decision.approved is False
    assert RejectReason.MAX_SYMBOL_EXPOSURE_EXCEEDED.value in failure_codes(decision.reasons)


def test_symbol_without_quote_falls_back_to_cost_basis(
    small_intent: OrderIntent,
    decision_now: datetime,
) -> None:
    held = PortfolioState(
        equity=Decimal("10000"),
        positions=(Position(symbol="ETH/USDT", qty=Decimal("1"), avg_entry=Decimal("2000")),),
    )
    btc_only = MarketContext(
        quotes=(
            MarketQuote(
                symbol="BTC/USDT",
                price=Decimal("30000"),
                as_of=decision_now,
            ),
        ),
        now=decision_now,
    )

    decision = evaluate(small_intent, held, exposure_limits("50", "50"), btc_only)

    # 2 000 (ETH at cost, no quote) + 3 000 = 5 000 == 50 % cap -> allowed.
    assert decision.approved is True


def test_cap_may_be_touched_exactly(
    small_intent: OrderIntent,
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    # notional 3 000 == 30 % of 10 000, for both caps.
    assert evaluate(small_intent, portfolio, exposure_limits("30", "30"), market).approved is True


def test_short_position_uses_absolute_exposure(
    make_intent: Callable[..., OrderIntent],
    portfolio: PortfolioState,
    market: MarketContext,
) -> None:
    held = PortfolioState(
        equity=Decimal("10000"),
        positions=(Position(symbol="BTC/USDT", qty=Decimal("-0.2"), avg_entry=Decimal("30000")),),
    )
    intent = make_intent(
        side="SELL",
        stop_loss=Decimal("31000"),
        take_profit=None,
        qty=Decimal("0.1"),
    )

    decision = evaluate(intent, held, exposure_limits("100", "50"), market)

    assert decision.approved is False  # |-0.2| * 30 000 + 3 000 = 9 000 > 5 000
    assert RejectReason.MAX_SYMBOL_EXPOSURE_EXCEEDED.value in failure_codes(decision.reasons)
