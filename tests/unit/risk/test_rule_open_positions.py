"""Rule 9: the book may hold only ``max_open_positions`` ideas at a time.

The cap exists so a multi-symbol scan cannot become a multi-position bet: exposure
caps would eventually stop it, but the order in which symbols were scanned would
decide which ones got in. A reducing order never needs a slot — refusing it would
trap a position behind the very limit that closed the book.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from aegis_core import (
    MarketContext,
    OrderIntent,
    PortfolioState,
    Position,
    RiskLimit,
    Side,
)
from aegis_risk import RejectReason, RiskCheck, evaluate

HELD = (
    Position(symbol="ETH/USDT", qty=Decimal("0.01"), avg_entry=Decimal("30000")),
    Position(symbol="SOL/USDT", qty=Decimal("0.01"), avg_entry=Decimal("30000")),
    Position(symbol="XRP/USDT", qty=Decimal("0.01"), avg_entry=Decimal("30000")),
)


def full_book_limits(max_open_positions: int) -> RiskLimit:
    return RiskLimit(
        max_risk_per_trade_pct=Decimal("2"),
        max_daily_loss_pct=Decimal("5"),
        max_weekly_loss_pct=Decimal("10"),
        max_exposure_per_symbol_pct=Decimal("50"),
        max_total_exposure_pct=Decimal("100"),
        max_open_positions=max_open_positions,
        max_leverage=Decimal("1"),
        allowed_symbols=frozenset({"BTC/USDT"}),
    )


def failure_codes(reasons: tuple[str, ...]) -> set[str]:
    return {reason.split(":")[0] for reason in reasons}


def test_a_full_book_rejects_a_new_idea(
    make_intent: Callable[..., OrderIntent],
    market: MarketContext,
) -> None:
    decision = evaluate(
        make_intent(qty=Decimal("0.01")),
        PortfolioState(equity=Decimal("10000"), positions=HELD),
        full_book_limits(3),
        market,
    )

    assert decision.approved is False
    assert RejectReason.MAX_OPEN_POSITIONS_REACHED.value in failure_codes(decision.reasons)


def test_a_slot_left_approves_and_records_the_check(
    make_intent: Callable[..., OrderIntent],
    market: MarketContext,
) -> None:
    decision = evaluate(
        make_intent(qty=Decimal("0.01")),
        PortfolioState(equity=Decimal("10000"), positions=HELD[:2]),
        full_book_limits(3),
        market,
    )

    assert decision.approved is True
    assert RiskCheck.OPEN_POSITIONS_WITHIN_LIMIT.value in decision.reasons


def test_a_reducing_order_is_allowed_on_a_full_book(
    make_intent: Callable[..., OrderIntent],
    market: MarketContext,
) -> None:
    held = (
        Position(symbol="BTC/USDT", qty=Decimal("0.1"), avg_entry=Decimal("30000")),
        *HELD,
    )
    reducing = make_intent(
        side=Side.SELL,
        qty=Decimal("0.1"),
        stop_loss=Decimal("31000"),
        take_profit=Decimal("28000"),
    )

    decision = evaluate(
        reducing,
        PortfolioState(equity=Decimal("10000"), positions=held),
        full_book_limits(3),
        market,
    )

    assert decision.approved is True
    assert RiskCheck.RISK_REDUCING_ORDER.value in decision.reasons
