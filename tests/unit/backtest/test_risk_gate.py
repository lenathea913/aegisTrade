"""A gated backtest runs the same limits the live gate would apply."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aegis_backtest import BacktestConfig, BacktestResult, run
from aegis_core import Candle, RiskLimit, Timeframe
from aegis_strategy import EmaCrossStrategy

SYMBOL = "BTC/USDT"


def _candles(count: int = 240) -> tuple[Candle, ...]:
    """Alternating 30-bar up/down trends, which force repeated EMA crossovers."""
    price = Decimal("100")
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    candles: list[Candle] = []
    for index in range(count):
        step = Decimal("0.01") if (index // 30) % 2 == 0 else Decimal("-0.01")
        open_price = price
        close_price = price * (Decimal("1") + step)
        candles.append(
            Candle(
                symbol=SYMBOL,
                timeframe=Timeframe.H1,
                open_time=moment,
                close_time=moment + timedelta(hours=1),
                open=open_price,
                high=max(open_price, close_price) * Decimal("1.002"),
                low=min(open_price, close_price) * Decimal("0.998"),
                close=close_price,
                volume=Decimal("1"),
            )
        )
        price = close_price
        moment += timedelta(hours=1)
    return tuple(candles)


def _limits(**overrides: object) -> RiskLimit:
    values: dict[str, object] = {
        "max_risk_per_trade_pct": Decimal("0.5"),
        "max_daily_loss_pct": Decimal("2"),
        "max_weekly_loss_pct": Decimal("5"),
        "max_exposure_per_symbol_pct": Decimal("20"),
        "max_total_exposure_pct": Decimal("60"),
        "max_leverage": Decimal("1"),
        "allowed_symbols": frozenset({SYMBOL}),
    }
    values.update(overrides)
    return RiskLimit(**values)


def _run(config: BacktestConfig) -> BacktestResult:
    strategy = EmaCrossStrategy(symbol=SYMBOL, timeframe=Timeframe.H1)
    return run(strategy, _candles(), config)


def test_gate_is_off_by_default() -> None:
    result = _run(BacktestConfig())

    assert result.config.risk_gate_enabled is False
    assert result.rejected_intents == ()
    assert result.total_trades > 0


def test_ungated_runs_are_unchanged() -> None:
    default = _run(BacktestConfig())
    explicit_off = _run(BacktestConfig(risk_gate_enabled=False))

    assert default.total_trades == explicit_off.total_trades
    assert default.final_equity == explicit_off.final_equity


def test_permissive_limits_approve_every_intent() -> None:
    result = _run(BacktestConfig(risk_gate_enabled=True, risk_limits=_limits()))

    assert result.rejected_count == 0
    assert result.total_trades > 0
    assert result.total_intents == result.total_trades


def test_tiny_risk_cap_rejects_every_entry_and_trades_nothing() -> None:
    result = _run(
        BacktestConfig(
            risk_gate_enabled=True,
            risk_limits=_limits(max_risk_per_trade_pct=Decimal("0.0001")),
        )
    )

    assert result.total_trades == 0
    assert result.rejected_count > 0
    assert all(
        any(reason.startswith("risk_per_trade_exceeded") for reason in rejected.reasons)
        for rejected in result.rejected_intents
    )


def test_symbol_allow_list_is_enforced_in_the_backtest() -> None:
    result = _run(
        BacktestConfig(
            risk_gate_enabled=True,
            risk_limits=_limits(allowed_symbols=frozenset({"ETH/USDT"})),
        )
    )

    assert result.total_trades == 0
    assert result.rejected_count > 0
    assert all(
        any(reason.startswith("symbol_not_allowed") for reason in rejected.reasons)
        for rejected in result.rejected_intents
    )


def test_default_limits_are_derived_from_the_traded_symbol() -> None:
    """No explicit limits: the gate still runs, allowing only the traded symbol."""
    result = _run(BacktestConfig(risk_gate_enabled=True))

    assert result.rejected_count == 0
    assert result.total_trades > 0
