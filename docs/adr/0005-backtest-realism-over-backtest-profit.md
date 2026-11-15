# ADR 0005 — Backtests must be pessimistic, not flattering

- **Status:** accepted
- **Date:** 2026-11-15

## Context

A backtest that looks good because of optimistic assumptions is worse than no
backtest: it manufactures confidence. The most common distortions are look-ahead
fills, ignored costs, and stop-losses that magically fill at their exact level.

## Decision

`aegis_backtest` models the frictions that decide whether a strategy survives:

1. **No look-ahead.** A signal computed from a closed bar is timestamped at that
   bar's close and can only fill at the **open of a later bar**
   (`1 + latency_bars`). There is a test asserting this timing.
2. **Costs always apply**: taker fee on market fills and exits, maker fee on touched
   limit entries, adverse slippage in basis points, funding charged on 8-hour
   boundaries for held notional.
3. **LIMIT orders fill only if the bar touched the limit**, and then at the limit
   price; untouched orders are dropped rather than assumed filled.
4. **Stop-loss has priority over take-profit** when a single bar contains both,
   which is the pessimistic reading.
5. **One position at a time, no pyramiding**; shorts are opt-in
   (`allow_short: false` by default).
6. **Results are descriptive.** `aegis_metrics` computes win rate, expectancy,
   profit factor, drawdown, Sharpe and Sortino, and `undefined` stays `None` instead
   of being replaced by a flattering number. No document claims an edge from them.

Known gap, documented rather than hidden: gap-through-stop is not modelled — an exit
fills at the stop/TP price plus slippage even if the bar opened beyond it, which is
optimistic for large gaps.

## Consequences

- Demo results look worse than a naive simulation, by design.
- Synthetic seed data exists so the pipeline runs offline; it is explicitly not
  market data and not evidence of anything.
- Any future "improvement" that raises returns must first justify itself as a more
  realistic assumption, not a kinder one.

## Alternatives considered

- **Vectorised signal backtest with close-to-close fills.** Rejected: classic
  look-ahead; results were unusably optimistic.
- **Borrowing a framework (vectorbt, backtesting.py).** Rejected for the MVP:
  the platform's value is auditability of its own assumptions.
