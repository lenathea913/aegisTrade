"""Run the demo EMA-cross backtest on synthetic seed data.

Prints descriptive statistics only. They describe this one synthetic run; they are
not evidence of edge and must not be read as expected performance.

Usage:
    python scripts/run_demo_backtest.py [--csv PATH]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from aegis_backtest import BacktestConfig, load_candles_csv, run
from aegis_core import Timeframe
from aegis_strategy import EmaCrossStrategy

DEFAULT_CSV = Path("data/seeds/BTCUSDT_1h.csv")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    args = parser.parse_args()

    candles = load_candles_csv(args.csv, symbol="BTC/USDT", timeframe=Timeframe.H1)
    strategy = EmaCrossStrategy(symbol="BTC/USDT", timeframe=Timeframe.H1)
    result = run(strategy, candles, BacktestConfig())

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
    print()
    print("These numbers describe one synthetic run. They are not a forecast and")
    print("not a claim about live results.")


if __name__ == "__main__":
    main()
