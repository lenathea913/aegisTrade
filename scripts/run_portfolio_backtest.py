"""Does the ten-symbol book actually make money with three slots and a real ranking?

Ten separate per-symbol backtests cannot answer that: each one assumes the symbol had
the whole account and no competition. Here all symbols share one $10 000 equity, the
20 %/60 % caps and ``max_open_positions = 3``, and entries are ranked by signal
strength — the same rule the runner now uses.

Three variants are printed on the same data and costs:

``portfolio (ranked)``   the deployed rule: strongest signal gets the slot;
``portfolio (alphabet)`` the same run with the old ticker order, which is what the
                         fixed 2.0 reward:risk degenerated into;
``BTC only``             the baseline the multi-symbol expansion was supposed to beat.

Data comes from ``market.candles`` (``--source db``, default) or from the monthly 1h
archives already cached in ``artifacts/universe_cache`` (``--source cache``), which is
the only source with three years for all ten symbols.

Usage:
    python scripts/run_portfolio_backtest.py
    python scripts/run_portfolio_backtest.py --source cache
    python scripts/run_portfolio_backtest.py --symbols BTC/USDT ETH/USDT --max-open-positions 2
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
)
from portfolio_sim import PortfolioResult, PortfolioRules, run_portfolio

from aegis_backtest import BacktestTrade
from aegis_config import load_settings
from aegis_core import Candle

DEFAULT_SYMBOLS = (
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "ZEC/USDT",
    "XRP/USDT",
    "BNB/USDT",
    "DOGE/USDT",
    "NEAR/USDT",
    "1000PEPE/USDT",
    "HYPE/USDT",
)
#: 50/200 needs 201 bars before it can speak; below that a "run" is just warmup.
MIN_BARS = 260


def _profit_factor(trades: tuple[BacktestTrade, ...]) -> str:
    wins = sum((max(trade.net_pnl, Decimal("0")) for trade in trades), Decimal("0"))
    losses = sum((min(trade.net_pnl, Decimal("0")) for trade in trades), Decimal("0"))
    if losses == 0:
        return "inf" if wins > 0 else "n/a"
    return f"{wins / abs(losses):.2f}"


def _load(symbols: list[str], args: argparse.Namespace) -> tuple[dict[str, list[Candle]], str]:
    series: dict[str, list[Candle]] = {}
    if args.source == "cache":
        start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
        end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
        for symbol in symbols:
            candles = load_from_cache(symbol, start, end, Path(args.cache_dir))
            if len(candles) >= MIN_BARS:
                series[symbol] = candles
            else:
                print(f"skip {symbol}: {len(candles)} cached bars", file=sys.stderr)
        return series, f"{args.start} -> {args.end} (archive cache)"

    environment = args.environment
    for symbol in symbols:
        candles = load_from_database(symbol, args.timeframes[0], environment)
        if len(candles) >= MIN_BARS:
            series[symbol] = candles
        else:
            print(f"skip {symbol}: {len(candles)} stored bars", file=sys.stderr)
    return series, f"market.candles ({environment})"


def _rules(args: argparse.Namespace) -> PortfolioRules:
    return PortfolioRules(
        initial_equity=args.equity,
        max_open_positions=args.max_open_positions,
        max_exposure_per_symbol_pct=args.per_symbol_pct,
        max_total_exposure_pct=args.total_pct,
        risk_per_trade_pct=args.risk_per_trade_pct,
        taker_fee_bps=args.taker_fee_bps,
        slippage_bps=args.slippage_bps,
        funding_bps_per_8h=args.funding_bps,
        allow_short=not args.no_short,
    )


def _row(label: str, result: PortfolioResult) -> dict[str, str]:
    metrics = result.metrics
    return {
        "variant": label,
        "trades": str(metrics.total_trades),
        "win rate": f"{metrics.win_rate:.1%}",
        "PF": _profit_factor(result.trades),
        "max DD": f"{metrics.max_drawdown:.1%}",
        "fees": f"{result.total_fees:.2f}",
        "funding": f"{result.total_funding:.2f}",
        "equity": f"{result.final_equity:.2f}",
        "return": f"{result.total_return_pct:+.2f}%",
        "no slot": str(result.rejected_for_slots),
        "no room": str(result.rejected_for_room),
    }


def _print_table(rows: list[dict[str, str]]) -> None:
    columns = list(rows[0])
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
    parser.add_argument("--start", default="2023-11-01", help="cache window start (UTC)")
    parser.add_argument("--end", default="2026-10-31 23:00", help="cache window end (UTC)")
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--equity", type=Decimal, default=Decimal("10000"))
    parser.add_argument("--max-open-positions", type=int, default=3)
    parser.add_argument("--risk-per-trade-pct", type=Decimal, default=Decimal("0.5"))
    parser.add_argument("--per-symbol-pct", type=Decimal, default=Decimal("20"))
    parser.add_argument("--total-pct", type=Decimal, default=Decimal("60"))
    parser.add_argument("--taker-fee-bps", type=Decimal, default=Decimal("7"))
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal("10"))
    parser.add_argument("--funding-bps", type=Decimal, default=Decimal("1"))
    parser.add_argument("--no-short", action="store_true")
    args = parser.parse_args(argv)

    settings = load_settings()
    args.timeframes = settings.market_data.timeframes
    symbols = args.symbols or list(settings.market_data.symbols) or list(DEFAULT_SYMBOLS)
    if args.source == "cache" and not cache_available(Path(args.cache_dir)):
        print(
            f"no cached archives in {args.cache_dir}; run the 1h download once first",
            file=sys.stderr,
        )
        return 1

    series, window = _load(symbols, args)
    if not series:
        print("no symbol has enough history for this window", file=sys.stderr)
        return 1

    rules = _rules(args)
    print(
        f"data            : {len(series)} symbols, {window}\n"
        f"rules           : equity {rules.initial_equity}, "
        f"max_open_positions {rules.max_open_positions}, "
        f"{rules.max_exposure_per_symbol_pct}%/symbol, {rules.max_total_exposure_pct}% total, "
        f"risk {rules.risk_per_trade_pct}%/trade\n"
        f"costs           : taker {rules.taker_fee_bps} bps, slippage "
        f"{rules.slippage_bps} bps, funding {rules.funding_bps_per_8h} bps/8h, "
        f"shorts {'off' if args.no_short else 'on'}, 1x isolated"
    )
    for symbol, candles in sorted(series.items()):
        print(
            f"  {symbol:<14}{len(candles):>7} bars  "
            f"{candles[0].open_time:%Y-%m-%d} -> {candles[-1].open_time:%Y-%m-%d}"
        )
    common_start = max(candles[0].open_time for candles in series.values())
    common_end = min(candles[-1].open_time for candles in series.values())
    print(f"all ten trade together from {common_start:%Y-%m-%d} to {common_end:%Y-%m-%d} UTC")
    print()

    ranked = run_portfolio(series, rules)
    alphabet = run_portfolio(series, rules, rank_by_strength=False)
    btc = series.get("BTC/USDT")
    btc_only = run_portfolio({"BTC/USDT": btc}, rules) if btc else None

    rows = [
        _row("portfolio (ranked)", ranked),
        _row("portfolio (alphabet)", alphabet),
    ]
    if btc_only is not None:
        rows.append(_row("BTC only", btc_only))
    _print_table(rows)
    print()

    print(
        f"slot contention : ranked run refused {ranked.rejected_for_slots} entry signal(s) "
        f"for want of a slot and {ranked.rejected_for_room} for want of exposure room, "
        f"and ignored {ranked.skipped_pyramid} repeat signal(s) on a held symbol."
    )
    if ranked.metrics.total_trades:
        avg_r = ranked.metrics.avg_r
        average_r = "n/a" if avg_r is None else f"{avg_r:.2f}"
        print(
            f"per trade       : {ranked.metrics.expectancy:.2f} expectancy, {average_r} average R"
        )
    print("These numbers describe one historical sample, not a forecast.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
