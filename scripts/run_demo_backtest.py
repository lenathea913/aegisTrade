"""Run the demo EMA-cross backtest on synthetic seed data.

Prints descriptive statistics only. They describe this one synthetic run; they are
not evidence of edge and must not be read as expected performance.

Usage:
    python scripts/run_demo_backtest.py [--csv PATH]
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from aegis_backtest import BacktestConfig, load_candles_csv, run
from aegis_core import RiskLimit, Timeframe
from aegis_strategy import EmaCrossParams, EmaCrossStrategy

DEFAULT_CSV = Path("data/seeds/BTCUSDT_1h.csv")

#: Mirrors configs/base.yaml ``risk.limits`` so a gated demo is judged by the same
#: numbers the paper stack would apply. Percentages are percent units.
DEMO_LIMITS = RiskLimit(
    max_risk_per_trade_pct=Decimal("0.5"),
    max_daily_loss_pct=Decimal("2"),
    max_weekly_loss_pct=Decimal("5"),
    max_exposure_per_symbol_pct=Decimal("20"),
    max_total_exposure_pct=Decimal("60"),
    max_leverage=Decimal("1"),
    allowed_symbols=frozenset({"BTC/USDT"}),
)


def _rejection_counts(result: object) -> dict[str, int]:
    """Count rejections by their first reason code (the actionable one)."""
    counts: dict[str, int] = {}
    for rejected in result.rejected_intents:  # type: ignore[attr-defined]
        first = rejected.reasons[0].split(":", 1)[0]
        counts[first] = counts.get(first, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument(
        "--risk-gate",
        action="store_true",
        help="evaluate every entry intent through RiskGate (shows what live limits would block)",
    )
    args = parser.parse_args()

    candles = load_candles_csv(args.csv, symbol="BTC/USDT", timeframe=Timeframe.H1)
    # A gated run sizes the position to the configured risk cap, so the demo shows
    # which signals the live limits accept instead of rejecting every one of them.
    params = EmaCrossParams(qty=Decimal("0.05")) if args.risk_gate else None
    strategy = EmaCrossStrategy(symbol="BTC/USDT", timeframe=Timeframe.H1, params=params)
    config = BacktestConfig(
        risk_gate_enabled=args.risk_gate,
        risk_limits=DEMO_LIMITS if args.risk_gate else None,
    )
    result = run(strategy, candles, config)

    metrics = result.metrics
    print(f"strategy          : {result.strategy_id}")
    print(f"data              : {len(candles)} bars of {result.symbol} {result.timeframe}")
    print(
        f"assumptions       : taker {result.config.taker_fee_bps} bps, "
        f"slippage {result.config.slippage_bps} bps, "
        f"latency {result.config.latency_bars} bars"
    )
    print(f"initial equity    : {result.config.initial_equity}")
    print(f"final equity      : {result.final_equity}")
    print(
        f"trades            : {metrics.total_trades} "
        f"(win {metrics.winning_trades} / loss {metrics.losing_trades})"
    )
    print(f"win rate          : {metrics.win_rate:.2%}")
    print(f"expectancy        : {metrics.expectancy:.2f} per trade")
    print(f"profit factor     : {metrics.profit_factor}")
    print(f"max drawdown      : {metrics.max_drawdown:.2%}")
    print(f"sharpe / sortino  : {metrics.sharpe} / {metrics.sortino}")
    print(f"avg R             : {metrics.avg_r}")
    print(f"fees / funding    : {result.total_fees} / {result.total_funding}")
    if result.config.risk_gate_enabled:
        print(
            f"risk gate         : on — {result.rejected_count} of "
            f"{result.total_intents} intents would have been refused"
        )
        for reason, count in _rejection_counts(result).items():
            print(f"  refused {count:>3}x {reason}")
    else:
        print("risk gate         : off (pass --risk-gate to judge the signals with it)")
    print()
    print("These numbers describe one synthetic run. They are not a forecast and")
    print("not a claim about live results.")


if __name__ == "__main__":
    main()
