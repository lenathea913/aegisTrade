"""Audit the backtest's first trade for look-ahead bias, bar by bar.

A summary file cannot prove that the engine did not peek at the future: the numbers
are computed *by* the code under suspicion. This script re-derives one trade from the
stored candles and shows, side by side:

* the bar whose **close** produced the signal (all the strategy was allowed to see),
* the **next** bar, and the price the engine actually filled at,

so an independent reader can check the claim themselves instead of trusting a log.

What "honest" means here:

* the signal is evaluated on bar *i* only after it closed (``candle.close_time``);
* the fill happens at ``bar i+1 + latency_bars`` and uses **that bar's open**, moved by
  the configured adverse slippage;
* the fill price is inside the execution bar's high/low range.

Look-ahead would look like a fill at the signal bar's close, low or high. Those prices
are printed too, with the edge they would have handed the strategy, so the difference
is visible in percentage terms.

Usage::

    python scripts/audit_lookahead.py                       # BTC/USDT 1h, first trade
    python scripts/audit_lookahead.py --symbol ETH/USDT

Exits non-zero when the audited trade contradicts the timing rules, so the check can
gate a pipeline instead of being read by eye.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

# Sibling scripts import each other by module name: ``python scripts/foo.py`` puts this
# directory on ``sys.path``, which is how the other research scripts run too.
from backtest_data import load_from_database

from aegis_backtest import BacktestConfig, run
from aegis_core import Candle, Side, Timeframe
from aegis_strategy import EmaCrossParams, EmaCrossStrategy

__all__ = ["main"]

DEFAULT_SYMBOL = "BTC/USDT"
TIMEFRAME = Timeframe.H1
_BPS = Decimal("10000")
_HUNDRED = Decimal("100")


def _num(value: Decimal) -> str:
    """A Decimal without exponent or padded zeros (``32382.6``, not ``3.23826E+4``)."""
    return format(value.normalize(), "f")


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def _pct(edge: Decimal, reference: Decimal) -> str:
    if reference == 0:
        return "n/a"
    return f"{edge / reference * _HUNDRED:+.4f} %"


def _cheat(fill: Decimal, cheat_price: Decimal) -> str:
    """``cheat_price -> what the real fill paid relative to it``.

    A positive number means the honest fill was *worse* than that cheating price, so a
    look-ahead engine would have had that much of an advantage.
    """
    return f"{_num(cheat_price)} -> real fill {_pct(fill - cheat_price, cheat_price)}"


def _table(rows: Sequence[tuple[str, str]], left: str = "Item", right: str = "Value") -> str:
    """Render ``rows`` as a bordered two-column table, sized to its content."""
    width_left = max(len(left), *(len(key) for key, _ in rows))
    width_right = max(len(right), *(len(value) for _, value in rows))
    rule = f"+-{'-' * width_left}-+-{'-' * width_right}-+"
    lines = [rule, f"| {left:<{width_left}} | {right:<{width_right}} |", rule]
    lines.extend(f"| {key:<{width_left}} | {value:<{width_right}} |" for key, value in rows)
    lines.append(rule)
    return "\n".join(lines)


def _checks(passed: bool, label: str, detail: str) -> str:
    return f"[{'PASS' if passed else 'FAIL'}] {label}: {detail}"


def _load(symbol: str, environment: str) -> list[Candle]:
    candles = load_from_database(symbol, TIMEFRAME, environment)
    if not candles:
        raise SystemExit(
            f"no stored {TIMEFRAME.value} candles for {symbol} in environment "
            f"{environment!r}; run the collector or scripts/backfill_candles.py first"
        )
    return candles


def _signal_index(candles: Sequence[Candle], entry_time: datetime) -> int:
    """Index of the bar that produced the entry (the one before the fill bar)."""
    for index, candle in enumerate(candles):
        if candle.open_time == entry_time:
            if index == 0:
                raise SystemExit("the first trade filled on the very first bar: no signal bar")
            return index - 1
    raise SystemExit(f"no candle opens at the trade's entry time {_stamp(entry_time)}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="show one backtest trade against the candles it claims to have used"
    )
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help="stored symbol to audit")
    parser.add_argument(
        "--environment",
        default="testnet",
        help="venue tag of the stored candles (testnet|mainnet)",
    )
    parser.add_argument(
        "--slippage-bps",
        type=Decimal,
        default=Decimal("5"),
        help="adverse slippage the engine applies to a market fill (default 5)",
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=5000,
        help="bars to replay from the start (0 = all): the first trade must be inside it",
    )
    args = parser.parse_args(argv)

    stored = _load(args.symbol, args.environment)
    # The audit needs trade #1, nothing after it: bars are processed in order, so a
    # longer replay cannot change an earlier trade. Replaying all 27 000 stored bars
    # would only make the script slower than the thing it is checking.
    candles = stored if args.bars <= 0 or args.bars >= len(stored) else stored[: args.bars]
    params = EmaCrossParams()
    strategy = EmaCrossStrategy(symbol=args.symbol, timeframe=TIMEFRAME, params=params)
    config = BacktestConfig(slippage_bps=args.slippage_bps, latency_bars=0)
    result = run(strategy, candles, config)

    if not result.trades:
        print(
            f"no trade to audit: {strategy.strategy_id} produced none on "
            f"{len(candles)} bars of {args.symbol} {TIMEFRAME.value}"
        )
        return 1

    trade = result.trades[0]
    index = _signal_index(candles, trade.entry_time)
    signal_bar = candles[index]
    entry_bar = candles[index + 1]

    # Re-derive the signal from the same window the engine gave the strategy: bars up
    # to and including the signal bar, nothing after it.
    replay = EmaCrossStrategy(symbol=args.symbol, timeframe=TIMEFRAME, params=params)
    intent = replay.on_candle(signal_bar, candles[: index + 1])

    expected_fill = entry_bar.open * (Decimal("1") + args.slippage_bps / _BPS)
    if trade.side is Side.SELL:
        expected_fill = entry_bar.open * (Decimal("1") - args.slippage_bps / _BPS)
    inside_range = entry_bar.low <= trade.entry_price <= entry_bar.high
    at_next_open = trade.entry_time == entry_bar.open_time
    after_signal = trade.entry_time > signal_bar.close_time
    price_matches = trade.entry_price == expected_fill
    replayed = intent is not None and intent.side is trade.side
    honest = at_next_open and after_signal and price_matches and inside_range and replayed

    print(
        f"strategy      : {result.strategy_id} (EMA {params.fast_period}/{params.slow_period},"
        f" ADX >= {params.adx_threshold})"
    )
    print(
        f"data          : audited {len(candles)} of {len(stored)} stored bars of"
        f" {args.symbol} {TIMEFRAME.value} [{args.environment}]"
        f" {_stamp(candles[0].open_time)} -> {_stamp(candles[-1].close_time)}"
    )
    print(
        f"trade #1      : {trade.side.value} {_num(trade.qty)} {trade.symbol}"
        f" entered {_stamp(trade.entry_time)}"
    )
    print()

    print(
        _table(
            [
                ("Signal bar close (UTC)", _stamp(signal_bar.close_time)),
                ("Signal bar close price", _num(signal_bar.close)),
                ("Signal bar high / low", f"{_num(signal_bar.high)} / {_num(signal_bar.low)}"),
                ("Bars visible to strategy", f"{index + 1} (indices 0..{index})"),
                ("Execution bar open (UTC)", _stamp(entry_bar.open_time)),
                ("Execution bar open price", _num(entry_bar.open)),
                ("Execution bar high / low", f"{_num(entry_bar.high)} / {_num(entry_bar.low)}"),
                ("Modeled fill price", _num(trade.entry_price)),
                ("Engine fill time", _stamp(trade.entry_time)),
                ("Slippage applied", f"{args.slippage_bps} bps"),
            ]
        )
    )
    print()

    print(
        _table(
            [
                (
                    "Fill vs signal close",
                    f"{_pct(trade.entry_price - signal_bar.close, signal_bar.close)}"
                    f" (price {_num(trade.entry_price - signal_bar.close)})",
                ),
                ("Fill vs execution open", _num(trade.entry_price - entry_bar.open)),
                (
                    "Cheating entry @ signal close",
                    _cheat(trade.entry_price, signal_bar.close),
                ),
                ("Cheating entry @ signal low", _cheat(trade.entry_price, signal_bar.low)),
                ("Cheating entry @ signal high", _cheat(trade.entry_price, signal_bar.high)),
            ],
            left="Comparison",
            right="Value (real fill vs the cheating price)",
        )
    )
    print()

    print(
        _checks(
            at_next_open,
            "fill is on the bar after the signal",
            f"signal {_stamp(signal_bar.open_time)} -> fill {_stamp(trade.entry_time)}",
        )
    )
    print(
        _checks(
            after_signal,
            "the fill happened after the signal bar closed",
            f"fill {_stamp(trade.entry_time)} > signal close {_stamp(signal_bar.close_time)}",
        )
    )
    print(
        _checks(
            price_matches,
            "fill price is the execution open plus slippage",
            f"open {_num(entry_bar.open)} x (1 + {args.slippage_bps}/{_num(_BPS)})"
            f" = {_num(expected_fill)}",
        )
    )
    print(
        _checks(
            inside_range,
            "fill is inside the execution bar's range",
            f"{_num(entry_bar.low)} <= {_num(trade.entry_price)} <= {_num(entry_bar.high)}",
        )
    )
    print(
        _checks(
            replayed,
            "signal really exists on that bar",
            "replaying the strategy on candles[0..signal] reproduces the entry",
        )
    )
    print()

    if honest:
        print(
            "VERDICT: honest -- trade #1 filled at the open of the bar after the signal"
            f" ({_num(entry_bar.open)}), not at the signal bar's close"
            f" ({_num(signal_bar.close)}), low ({_num(signal_bar.low)}) or"
            f" high ({_num(signal_bar.high)})."
        )
        return 0
    print(
        "VERDICT: LOOK-AHEAD -- the fill does not match the next bar's open. Investigate"
        " packages/aegis_backtest/engine.py before trusting any backtest number."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
