"""1h vs 4h: does trading four times less often stop fees from eating the edge?

The 1h series is already stored, so the 4h series is **aggregated from it** (four
complete hourly bars per 4h bar, aligned to 00:00/04:00/... UTC) rather than downloaded
again. Same strategy, same 2 % stop / 4 % target / ADX 25, same costs, same notional:
the only thing that changes is how many times the bot pays the exchange.

Each row reports the fee bill next to the profit it produced, because that ratio is the
question: a strategy that wins 1.5x its losses before costs can still lose money at
34-44 bps round trip on every trade. Fewer, larger trades on 4h should move that
ratio; the table says by how much.

Usage:
    python scripts/run_timeframe_backtest.py
    python scripts/run_timeframe_backtest.py --source cache
    python scripts/run_timeframe_backtest.py --portfolio
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from backtest_data import (
    CACHE_DIR,
    cache_available,
    load_from_cache,
    load_from_database,
    resample,
)
from portfolio_sim import PortfolioRules, run_portfolio

from aegis_backtest import BacktestConfig, BacktestResult, run
from aegis_config import load_settings
from aegis_core import Candle, Timeframe
from aegis_strategy import EmaCrossParams, EmaCrossStrategy

MIN_BARS = 260
TIMEFRAMES = (Timeframe.H1, Timeframe.H4)


def _load(args: argparse.Namespace, symbols: list[str]) -> tuple[dict[str, list[Candle]], str]:
    series: dict[str, list[Candle]] = {}
    if args.source == "cache":
        start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
        end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
        for symbol in symbols:
            series[symbol] = load_from_cache(symbol, start, end, Path(args.cache_dir))
        return series, f"{args.start} -> {args.end} (archive cache)"

    for symbol in symbols:
        series[symbol] = load_from_database(symbol, Timeframe.H1, args.environment)
    return series, f"market.candles ({args.environment}), 1h aggregated to 4h"


def _measure(
    candles: list[Candle], symbol: str, timeframe: Timeframe, args: argparse.Namespace
) -> BacktestResult:
    params = EmaCrossParams(
        qty=args.notional / candles[0].open,
        adx_threshold=Decimal("25"),
    )
    config = BacktestConfig(
        initial_equity=args.equity,
        taker_fee_bps=args.taker_fee_bps,
        slippage_bps=args.slippage_bps,
        funding_rate_bps_per_8h=args.funding_bps,
        allow_short=not args.no_short,
        leverage=1,
    )
    strategy = EmaCrossStrategy(symbol=symbol, timeframe=timeframe, params=params)
    return run(strategy, candles, config)


def _row(label: str, symbol: str, result: BacktestResult, bars: int) -> dict[str, str]:
    metrics = result.metrics
    pf = "n/a" if metrics.profit_factor is None else f"{metrics.profit_factor:.2f}"
    fees_share = (
        f"{result.total_fees / Decimal(str(metrics.gross_profit)):.1%}"
        if metrics.gross_profit > 0
        else "n/a"
    )
    total_return = (result.final_equity / result.config.initial_equity - Decimal("1")) * Decimal(
        "100"
    )
    return {
        "tf": label,
        "symbol": symbol,
        "bars": str(bars),
        "trades": str(metrics.total_trades),
        "WR": f"{metrics.win_rate:.1%}",
        "PF": pf,
        "fees": f"{result.total_fees:.2f}",
        "fees/gross": fees_share,
        "max DD": f"{metrics.max_drawdown:.1%}",
        "return": f"{total_return:+.2f}%",
    }


def _synthetic(label: str, symbol: str, rows: list[dict[str, str]]) -> dict[str, str]:
    """Aggregate row: fees and trades add up; PF is recomputed from the trade PnLs."""
    trades = [trade for row in rows for trade in row["_trades"]]  # type: ignore[misc]
    raises = [trade.net_pnl for trade in trades if trade.net_pnl > 0]
    falls = [trade.net_pnl for trade in trades if trade.net_pnl < 0]
    gross = sum(raises, Decimal("0"))
    fees = sum((trade.fee for trade in trades), Decimal("0"))
    return {
        "tf": label,
        "symbol": symbol,
        "bars": "all",
        "trades": str(len(trades)),
        "WR": "n/a",
        "PF": "inf" if not falls else f"{gross / abs(sum(falls, Decimal('0'))):.2f}",
        "fees": f"{fees:.2f}",
        "fees/gross": f"{fees / gross:.1%}" if gross > 0 else "n/a",
        "max DD": "n/a",
        "return": "n/a",
    }


def _print_table(rows: list[dict[str, str]]) -> None:
    columns = [column for column in rows[0] if not column.startswith("_")]
    widths = {column: max(len(column), *(len(row[column]) for row in rows)) for column in columns}
    header = "  ".join(column.ljust(widths[column]) for column in columns)
    print(header)
    print("-" * len(header))
    for row in rows:
        print("  ".join(row[column].ljust(widths[column]) for column in columns))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("db", "cache"), default="db")
    parser.add_argument("--environment", default="testnet", choices=("testnet", "mainnet"))
    parser.add_argument("--cache-dir", default=str(CACHE_DIR))
    parser.add_argument("--start", default="2023-11-01")
    parser.add_argument("--end", default="2026-10-31 23:00")
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--equity", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--notional", type=Decimal, default=Decimal("3000"))
    parser.add_argument("--taker-fee-bps", type=Decimal, default=Decimal("7"))
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal("10"))
    parser.add_argument("--funding-bps", type=Decimal, default=Decimal("1"))
    parser.add_argument("--no-short", action="store_true")
    parser.add_argument(
        "--portfolio",
        action="store_true",
        help="also run the shared-book simulation (3 slots) on both timeframes",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    symbols = args.symbols or list(settings.market_data.symbols)
    if args.source == "cache" and not cache_available(Path(args.cache_dir)):
        print(f"no cached archives in {args.cache_dir}", file=sys.stderr)
        return 1

    hourly, window = _load(args, symbols)
    print(f"data : {window}")
    print(
        f"costs: taker {args.taker_fee_bps} bps, slippage {args.slippage_bps} bps, "
        f"funding {args.funding_bps} bps/8h, notional ${args.notional}, "
        f"shorts {'off' if args.no_short else 'on'}, 1x isolated"
    )
    print()

    products: dict[Timeframe, dict[str, list[Candle]]] = {Timeframe.H1: {}, Timeframe.H4: {}}
    rows: list[dict[str, str]] = []
    for symbol in symbols:
        base = hourly.get(symbol, [])
        if len(base) < MIN_BARS:
            print(f"skip {symbol}: {len(base)} hourly bars", file=sys.stderr)
            continue
        for timeframe in TIMEFRAMES:
            candles = base if timeframe is Timeframe.H1 else resample(base, timeframe)
            if len(candles) < MIN_BARS:
                print(
                    f"skip {symbol} {timeframe.value}: {len(candles)} bars after resampling",
                    file=sys.stderr,
                )
                continue
            products[timeframe][symbol] = candles
            result = _measure(candles, symbol, timeframe, args)
            row = _row(timeframe.value, symbol, result, len(candles))
            row["_trades"] = result.trades  # type: ignore[assignment]
            rows.append(row)

    if not rows:
        print("no symbol has enough history", file=sys.stderr)
        return 1

    for timeframe in TIMEFRAMES:
        subset = [row for row in rows if row["tf"] == timeframe.value]
        if subset:
            rows.append(_synthetic(f"{timeframe.value} total", "-", subset))

    _print_table(rows)
    print()

    for timeframe in TIMEFRAMES:
        subset = [row for row in rows if row["tf"] == timeframe.value]
        if not subset:
            continue
        per_symbol = [row["return"] for row in subset]
        print(f"{timeframe.value}: {len(subset)} symbols traded, returns {' '.join(per_symbol)}")

    if args.portfolio:
        print()
        print("shared book, 3 slots, 20%/60% caps:")
        rules = PortfolioRules(
            initial_equity=args.equity,
            taker_fee_bps=args.taker_fee_bps,
            slippage_bps=args.slippage_bps,
            funding_bps_per_8h=args.funding_bps,
            allow_short=not args.no_short,
        )
        portfolio_rows: list[dict[str, str]] = []
        for timeframe in TIMEFRAMES:
            series = products[timeframe]
            if not series:
                continue
            result = run_portfolio(series, rules)
            metrics = result.metrics
            fees = result.total_fees
            gross = Decimal(str(metrics.gross_profit))
            portfolio_rows.append(
                {
                    "tf": timeframe.value,
                    "symbols": str(len(series)),
                    "trades": str(metrics.total_trades),
                    "PF": "n/a"
                    if metrics.profit_factor is None
                    else f"{metrics.profit_factor:.2f}",
                    "fees": f"{fees:.2f}",
                    "fees/gross": f"{fees / gross:.1%}" if gross > 0 else "n/a",
                    "equity": f"{result.final_equity:.2f}",
                    "return": f"{result.total_return_pct:+.2f}%",
                }
            )
        _print_table(portfolio_rows)

    print()
    print("fees/gross is the fee bill as a share of gross winning profit: the lower it")
    print("is, the less of the edge the exchange takes. One historical sample, not a forecast.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
