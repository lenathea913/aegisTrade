"""Compare one EMA 12/26 backtest at 1x (spot-equivalent) against leveraged futures.

The question this answers is narrow and worth stating: *given the same costs and starting
equity, which EMA pair survives this market, and does leverage help or does it just make
the bad runs bigger?* The strategy's quantity is treated as the margin committed at 1x,
so at 3x the same collateral carries three times the notional; PnL, fees and funding
scale with it and the estimated isolated-margin liquidation price moves to
``entry * (1 -/+ 1/leverage +/- 0.5%)``.

Every EMA pair is run over the same bars with the same costs, so the only thing that
differs between rows is the strategy (and the leverage column).

Data comes from the committed seed CSV (1500 hourly bars) or, with ``--from-db``, from
``market.candles`` for the configured database.

Usage:
    python scripts/run_futures_backtest.py                 # seed data, 1x vs 3x
    python scripts/run_futures_backtest.py --from-db       # stored candles, 1x vs 3x
    python scripts/run_futures_backtest.py --leverage 1 --leverage 5
    python scripts/run_futures_backtest.py --from-db \
        --ema 12/26 --ema 3/5 --ema 9/21 --ema 50/200
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

from aegis_backtest import BacktestConfig, BacktestResult, load_candles_csv, run
from aegis_core import Candle, Timeframe
from aegis_strategy import EmaCrossParams, EmaCrossStrategy

DEFAULT_CSV = Path("data/seeds/BTCUSDT_1h.csv")
DEFAULT_SYMBOL = "BTC/USDT"


def _load_from_database(symbol: str, timeframe: Timeframe, environment: str) -> tuple[Candle, ...]:
    """Read one series from ``market.candles`` (the same table the runner trades)."""
    from sqlalchemy import select

    from aegis_config import load_settings
    from aegis_db import Candle as CandleRow
    from aegis_db import create_engine_from_settings, create_session_factory, session_scope

    settings = load_settings()
    engine = create_engine_from_settings(settings.database)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            rows = (
                session.execute(
                    select(CandleRow)
                    .where(
                        CandleRow.symbol == symbol,
                        CandleRow.timeframe == timeframe.value,
                        CandleRow.environment == environment,
                    )
                    .order_by(CandleRow.open_time)
                )
                .scalars()
                .all()
            )
            return tuple(
                Candle(
                    symbol=row.symbol,
                    timeframe=Timeframe(row.timeframe),
                    open_time=row.open_time,
                    close_time=row.close_time,
                    open=row.open,
                    high=row.high,
                    low=row.low,
                    close=row.close,
                    volume=row.volume,
                    trades=row.trades,
                )
                for row in rows
            )
    finally:
        engine.dispose()


def _parse_ema(raw: str) -> tuple[int, int]:
    """``fast/slow`` (e.g. ``9/21``) into a pair the parameters accept."""
    try:
        fast, slow = (int(part) for part in raw.split("/", 1))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected FAST/SLOW, got {raw!r}") from exc
    if fast >= slow:
        raise argparse.ArgumentTypeError(f"fast period must be shorter than slow in {raw!r}")
    return fast, slow


def _describe(result: BacktestResult, ema: tuple[int, int], leverage: int) -> dict[str, str]:
    metrics = result.metrics
    initial = result.config.initial_equity
    total_return = (result.final_equity / initial - Decimal("1")) * Decimal("100")
    profit_factor = "n/a" if metrics.profit_factor is None else f"{metrics.profit_factor:.2f}"
    sharpe = "n/a" if metrics.sharpe is None else f"{metrics.sharpe:.2f}"
    return {
        "ema": f"{ema[0]}/{ema[1]}",
        "leverage": f"{leverage}x",
        "trades": str(metrics.total_trades),
        "liquidations": str(result.liquidations),
        "win rate": f"{metrics.win_rate:.1%}",
        "profit factor": profit_factor,
        "max drawdown": f"{metrics.max_drawdown:.2%}",
        "sharpe": sharpe,
        "fees": f"{result.total_fees:.2f}",
        "funding": f"{result.total_funding:.2f}",
        "final equity": f"{result.final_equity:.2f}",
        "return": f"{total_return:.2f}%",
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
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="seed candle file")
    parser.add_argument(
        "--from-db",
        action="store_true",
        help="read market.candles instead of the seed file (needs the database settings)",
    )
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument(
        "--timeframe",
        default=Timeframe.H1.value,
        choices=[timeframe.value for timeframe in Timeframe],
    )
    parser.add_argument("--environment", default="testnet", choices=("testnet", "mainnet"))
    parser.add_argument("--equity", type=Decimal, default=Decimal("10000"))
    parser.add_argument(
        "--leverage",
        type=int,
        action="append",
        dest="leverages",
        help="leverage to run (repeatable); defaults to 1 and 3",
    )
    parser.add_argument(
        "--ema",
        type=_parse_ema,
        action="append",
        dest="emas",
        help="EMA fast/slow pair to run (repeatable), e.g. --ema 9/21; defaults to the "
        "strategy's own parameters",
    )
    parser.add_argument("--taker-fee-bps", type=Decimal, default=Decimal("5"))
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal("5"))
    parser.add_argument("--funding-bps-per-8h", type=Decimal, default=Decimal("1"))
    parser.add_argument(
        "--no-short",
        action="store_true",
        help="ignore SELL signals (spot behaviour); futures runs allow shorts by default",
    )
    args = parser.parse_args(argv)

    timeframe = Timeframe(args.timeframe)
    if args.from_db:
        candles = _load_from_database(args.symbol, timeframe, args.environment)
        source = f"market.candles ({args.environment})"
    else:
        candles = load_candles_csv(args.csv, symbol=args.symbol, timeframe=timeframe)
        source = str(args.csv)
    if not candles:
        print(f"no candles found in {source}", file=sys.stderr)
        return 1

    leverages = args.leverages or [1, 3]
    default_params = EmaCrossParams()
    emas = args.emas or [(default_params.fast_period, default_params.slow_period)]
    rows: list[dict[str, str]] = []
    runs: list[tuple[tuple[int, int], int, BacktestResult]] = []
    for ema in emas:
        for leverage in leverages:
            # Same bars, same quantity, same costs: only the EMA pair and leverage differ.
            params = EmaCrossParams(fast_period=ema[0], slow_period=ema[1], qty=Decimal("0.05"))
            strategy = EmaCrossStrategy(symbol=args.symbol, timeframe=timeframe, params=params)
            config = BacktestConfig(
                initial_equity=args.equity,
                taker_fee_bps=args.taker_fee_bps,
                slippage_bps=args.slippage_bps,
                funding_rate_bps_per_8h=args.funding_bps_per_8h,
                allow_short=not args.no_short,
                leverage=leverage,
            )
            result = run(strategy, candles, config)
            runs.append((ema, leverage, result))
            rows.append(_describe(result, ema, leverage))

    print(f"data            : {len(candles)} bars of {args.symbol} {timeframe} from {source}")
    print(
        f"first / last    : {candles[0].open_time:%Y-%m-%d %H:%M} -> "
        f"{candles[-1].close_time:%Y-%m-%d %H:%M} UTC"
    )
    print(
        f"assumptions     : taker {args.taker_fee_bps} bps, slippage {args.slippage_bps} bps, "
        f"funding {args.funding_bps_per_8h} bps/8h, "
        f"shorts {'off' if args.no_short else 'on'}, margin ISOLATED"
    )
    print(f"initial equity  : {args.equity}")
    print()
    _print_table(rows)
    print()

    for ema, leverage, result in runs:
        liquidated = [trade for trade in result.trades if trade.exit_reason.value == "liquidated"]
        label = f"EMA {ema[0]}/{ema[1]} @ {leverage}x"
        if liquidated:
            first = liquidated[0]
            print(
                f"{label}: {len(liquidated)} liquidation(s); first at "
                f"{first.exit_time:%Y-%m-%d %H:%M} UTC, exit {first.exit_price:.2f} "
                f"({first.net_pnl:.2f})"
            )
        else:
            print(f"{label}: no liquidation - every exit was a stop, target or signal")

    print()
    print("Liquidation prices are the simplified isolated-margin estimate")
    print("(entry * (1 -/+ 1/leverage +/- 0.5%)); the venue's own number is authoritative.")
    print("These numbers describe one historical sample, not a forecast.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
