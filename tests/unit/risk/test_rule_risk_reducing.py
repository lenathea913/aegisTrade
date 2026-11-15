"""A protective exit is judged by the gate, but not blocked by the new-risk caps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aegis_core import (
    MarketContext,
    MarketQuote,
    OrderIntent,
    OrderType,
    PortfolioState,
    Position,
    RiskLimit,
    Side,
)
from aegis_risk import RiskCheck, evaluate

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
ENTRY = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def _limits(**overrides: object) -> RiskLimit:
    values: dict[str, object] = {
        "max_risk_per_trade_pct": Decimal("0.5"),
        "max_daily_loss_pct": Decimal("2"),
        "max_weekly_loss_pct": Decimal("5"),
        "max_exposure_per_symbol_pct": Decimal("20"),
        "max_total_exposure_pct": Decimal("60"),
        "max_leverage": Decimal("1"),
        "allowed_symbols": frozenset({"BTC/USDT"}),
    }
    values.update(overrides)
    return RiskLimit(**values)


def _market(price: str = "29000") -> MarketContext:
    return MarketContext(
        quotes=(
            MarketQuote(symbol="BTC/USDT", price=Decimal(price), as_of=NOW - timedelta(seconds=5)),
        ),
        now=NOW,
    )


def _state(*, realized: str = "0", qty: str = "0.5") -> PortfolioState:
    positions = (
        Position(
            symbol="BTC/USDT",
            qty=Decimal(qty),
            avg_entry=Decimal("30000"),
            opened_at=ENTRY,
        ),
    )
    return PortfolioState(
        equity=Decimal("10000"),
        daily_realized_pnl=Decimal(realized),
        weekly_realized_pnl=Decimal(realized),
        positions=positions,
    )


def _exit(**overrides: object) -> OrderIntent:
    values: dict[str, object] = {
        "symbol": "BTC/USDT",
        "side": Side.SELL,
        "order_type": OrderType.MARKET,
        "qty": Decimal("0.5"),
        "price": Decimal("29000"),
        "stop_loss": Decimal("29500"),
        "strategy_id": "position_monitor",
        "reason": "position_monitor: stop_loss touched",
        "created_at": NOW - timedelta(seconds=10),
    }
    values.update(overrides)
    return OrderIntent(**values)


def test_protective_exit_is_approved_even_after_the_daily_loss_floor() -> None:
    """The cap that stops new risk must not trap a position that needs closing."""
    decision = evaluate(
        _exit(),
        _state(realized="-500"),  # 5 % daily loss against a 2 % limit
        _limits(),
        _market(),
    )

    assert decision.approved is True
    assert RiskCheck.RISK_REDUCING_ORDER in decision.reasons


def test_partial_reduction_is_also_risk_reducing() -> None:
    decision = evaluate(
        _exit(qty=Decimal("0.2"), stop_loss=Decimal("29500")),
        _state(realized="-500"),
        _limits(),
        _market(),
    )

    assert decision.approved is True
    assert RiskCheck.RISK_REDUCING_ORDER in decision.reasons


def test_exit_larger_than_the_position_keeps_every_cap() -> None:
    """Flipping through zero opens new exposure, so the caps must still apply."""
    decision = evaluate(
        _exit(qty=Decimal("0.6")),
        _state(realized="-500"),
        _limits(),
        _market(),
    )

    assert decision.approved is False
    assert RiskCheck.RISK_REDUCING_ORDER not in decision.reasons
    assert any(reason.startswith("daily_loss_limit_reached") for reason in decision.reasons)


def test_new_entry_is_still_blocked_by_the_loss_floor() -> None:
    intent = OrderIntent(
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        qty=Decimal("0.05"),
        price=Decimal("29000"),
        stop_loss=Decimal("28000"),
        strategy_id="simple_ema_cross",
        reason="crossed above",
        created_at=NOW,
    )

    decision = evaluate(intent, _state(realized="-500"), _limits(), _market())

    assert decision.approved is False
    assert any(reason.startswith("daily_loss_limit_reached") for reason in decision.reasons)


def test_reducing_order_still_needs_fresh_market_data() -> None:
    stale = MarketContext(
        quotes=(
            MarketQuote(
                symbol="BTC/USDT",
                price=Decimal("29000"),
                as_of=NOW.replace(hour=9),
            ),
        ),
        now=NOW,
    )

    decision = evaluate(_exit(), _state(), _limits(), stale)

    assert decision.approved is False
    assert any(reason.startswith("stale_market_data") for reason in decision.reasons)


def test_reducing_order_still_respects_the_kill_switch() -> None:
    decision = evaluate(_exit(), _state(), _limits(), _market(), kill_switch=True)

    assert decision.approved is False
    assert "kill_switch_active" in decision.reasons


def test_side_must_oppose_the_position_to_count_as_reducing() -> None:
    """A same-side intent is an add, never a reduction, whatever its size."""
    add = OrderIntent(
        symbol="BTC/USDT",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        qty=Decimal("0.1"),
        price=Decimal("29000"),
        stop_loss=Decimal("28000"),
        strategy_id="simple_ema_cross",
        reason="adding",
        created_at=NOW,
    )

    decision = evaluate(add, _state(realized="-500"), _limits(), _market())

    assert decision.approved is False
    assert RiskCheck.RISK_REDUCING_ORDER not in decision.reasons
