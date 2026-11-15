# ADR 0009 — Protective exits go through the gate, with a risk-reducing exemption

- **Status:** accepted
- **Date:** 2026-11-15

## Context

The risk gate governed entries only. A position was opened with a stop recorded in the
order row and the audit trail, but nothing watched the market and closed it — so the
"stop" was documentation, not protection. That is acceptable for a research backtest
and unacceptable for paper trading, let alone live trading.

Closing a position is also an *order*, so it must not get a private door around the
gate. But an exit is not new risk: applying the daily-loss halt or the exposure caps to
it would trap an open position once a limit was hit, turning a bounded loss into an
unbounded one. Both constraints are real, so the rules had to distinguish the two
cases.

## Decision

1. **`apps/position_monitor` watches protected positions.** Every check reads the open
   positions and their levels from the ledger, compares the latest *closed* candle with
   the stop and target, and sends a MARKET closing intent for the full position when
   one is touched. It runs its own loop (`make monitor-run`) and never talks to an
   exchange directly.
2. **Protection levels live with the position.** `PortfolioLedger` keeps a
   `PositionProtection` (side, qty, stop, target) set by `TradingPipeline` from the
   approved order and dropped when the position goes flat — a flat position cannot
   carry a stop, and a stale stop would fire on exposure that no longer exists.
3. **Exits are ordinary intents through the same pipeline.** They are judged by
   `RiskGate`, executed by `ExecutionGateway`, recorded by the ledger and written to the
   audit trail. A rejected exit is a normal `risk_rejected` event, visible in `/journal`.
4. **The gate recognises risk-reducing orders.** An intent whose side opposes the
   current position and whose quantity does not exceed it skips the caps that limit
   *new* risk: risk-per-trade, daily/weekly loss floors, total and per-symbol exposure,
   and leverage. It still has to pass the kill switch, the symbol allow-list, the
   market-data freshness rule, the entry price and the stop-side check.
5. **The exemption is computed from the portfolio snapshot**, never from a flag the
   caller sets. A caller cannot declare its own order risk-reducing.
6. **The kill switch still blocks exits.** An operator halt means "stop touching the
   market"; automatically closing while halted would contradict that instruction, so
   open positions are the operator's responsibility during a halt and the runbook says so.

## Consequences

**Positive**

- A stop is now enforced by code, not by a row in a table.
- A loss limit can no longer lock in an open loss, which was the worse failure of the two.
- Exits inherit the audit trail, so the journal explains every close as precisely as
  every open.

**Negative**

- The intent→verdict mapping is no longer purely "one set of rules for all orders": one
  branch depends on existing state, so tests must cover both branches (they do).
- The monitor only knows what its own process restored. Position reload from the tables
  is implemented for the monitor's startup (positions plus newest stop/target), but
  realized PnL history is not reconstructed, so its loss windows restart at zero.
- Monitoring a bar-based price is coarser than a tick feed: a stop is acted on when a
  closed candle shows the level was crossed, not the instant it trades through.

## Alternatives considered

- **Exchange-side stop orders.** Not available: there is no live adapter, and the paper
  broker has no resting-order book.
- **A second, risk-free "close" path bypassing the gate.** Rejected: it would be the
  first exception to ADR 0003, and exceptions become the norm.
- **Close first, ask forgiveness later (log after the fact).** Rejected: an unapproved
  close would be invisible to the risk budget and to the audit chain.
