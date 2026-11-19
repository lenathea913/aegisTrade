# Runbook — reconcile the ledger with the database

**Use when:** `/positions` or `/risk/budget` looks wrong, the platform was restarted,
a restore happened, or you suspect the in-process ledger and the tables disagree.

The ledger is the source of truth **while a process runs**; the tables are the durable
record. Both processes rebuild the book from `trading.trades` at startup, and an open
position's stop/target is reloaded from `trading.positions`, so a restart no longer
silently drops protection. `/positions` can still differ from the tables while a run
is in flight (an unpersisted tick, a slow replica) — that is the most common source of
a "mismatch".

## 1. Snapshot both sides

```bash
curl -s localhost:8000/positions | jq
curl -s localhost:8000/risk/budget | jq '.equity, .open_positions'
```

```bash
docker compose -f infra/compose/docker-compose.paper.yml exec postgres \
  psql -U aegis -d aegis -c \
  "select symbol, qty, avg_entry, realized_pnl, updated_at from trading.positions order by symbol;"
```

## 2. Arithmetic checks

1. **Trade count vs order fills**: every FILLED order should have at least one trade.

   ```sql
   select o.order_id, o.status, count(t.trade_id) as trades
   from trading.orders o
   left join trading.trades t on t.order_id = o.order_id
   group by 1, 2 having count(t.trade_id) = 0 and o.status = 'FILLED';
   ```

   A row here is a real defect: the ledger recorded a fill whose trade row is missing.

2. **Fees and funding**: net costs must equal `sum(fee) - sum(funding)` over trades.
   Equity must be `initial_equity + realized_pnl - fees + funding + unrealized`.

3. **Position signs**: `qty` is signed (short is negative). A short position with
   positive `avg_entry` is normal; a flat position must have `avg_entry = 0` (the
   database CHECK enforces it).

4. **Audit completeness**: for each `trade_recorded` event there should be a trade
   row with that id.

   ```sql
   select entity_id from audit.audit_logs
   where event_type = 'trade_recorded'
     and entity_id not in (select trade_id from trading.trades);
   ```

## 3. If they disagree

1. **Stop trading**: kill switch on (`docs/runbooks/kill-switch.md`).
2. Preserve evidence: take a backup before any repair
   (`docs/runbooks/backup-restore.md`).
3. Never `UPDATE` or `DELETE` audit rows; the trigger will refuse, and forcing it
   would destroy the only trustworthy record.
4. Determine which side is wrong:
   - trades exist but the ledger disagrees → the ledger lost state (restart); rebuild
     from the tables and `risk.initial_equity`.
   - ledger moved but rows are missing → a persistence failure; investigate the
     process log around the timestamp, then correct forward with a compensating entry
     rather than rewriting history.
5. Record what happened in the incident notes, including the SQL you ran.

## 4. Venue-side comparison (futures, testnet)

A disagreement with the *database* is not the dangerous one; a disagreement with the
**exchange** is, because the next order is sized against whatever the ledger believes.
The runner compares the book with `GET /fapi/v2/positionRisk` every
`reconciler.position_interval_seconds` (default 60 s) and, on any difference in net
quantity, entry price (> 0.5 %) or leverage:

1. logs `position_drift_detected` at **CRITICAL**,
2. sends a CRITICAL alert (Telegram when `alerting.enabled`),
3. writes `trading.reconciliation_state` with `entries_halted = true`.

### A stop that fired is a fill, not a drift

Before any of the above, the reconciler asks the venue what it executed
(`GET /fapi/v1/allOrders` for `STOP_MARKET`/`TAKE_PROFIT_MARKET`, then
`GET /fapi/v1/userTrades`). When those executions replay onto the book and land exactly
on the venue's quantity, they are booked as trades with `strategy_id =
'venue_protection'` — an order row, the trade(s), the new position and `trade_recorded`
audit events, all in one transaction — and **no drift is raised**. The log line is
`venue_fill_reconciled`.

A drift is therefore only reported when the difference is left unexplained: a fill that
would grow or flip the book, a total that does not match the venue, or a venue history
that could not be read. Those still halt entries.

What that means operationally:

- **New entries stop immediately** — the runner logs `runner_entries_halted` and skips
  its scan. The protective monitor keeps running: an open position still needs its
  stop, and the venue also holds `STOP_MARKET`/`TAKE_PROFIT_MARKET` orders if they were
  placed successfully.
- **The header badge turns red** — "Ledger drift!" with the drifted symbol count.
- Everything else (journal, signals, manual close) still works.

### Acknowledging

Only after the book and the venue agree again:

```bash
# 1. See what the last comparison found
psql "$AEGIS_DATABASE__URL" -c \
  "select status, entries_halted, drift_count, detail, detected_at from trading.reconciliation_state;"

# 2. Look at the venue by hand (web UI or API), and at the audit trail
psql "$AEGIS_DATABASE__URL" -c \
  "select created_at, entity_id, payload from audit.audit_logs where event_type = 'position_drift_detected' order by id desc limit 5;"

# 3. If the difference was a missed fill, record the correction forward
#    (see section 3) — never rewrite trades or audit rows.

# 4. Clear the halt; the next comparison re-checks the venue within a minute
PYTHONPATH="packages;apps" uv run python -m order_reconciler --acknowledge-drift
```

The acknowledgement is written to the audit log as `position_drift_acknowledged`
(actor `operator`). Clearing the flag is not evidence that the venue agrees: the status
becomes `unchecked` and the next pass decides again.

If the badge says "Ledger unchecked", no comparison has run yet (paper mode, spot mode,
or the runner has not started) — that is not a drift.

## 5. Protective levels and venue stops after a restart

Open positions store `stop_loss`/`take_profit` **and** the venue order ids of those exits
(`stop_order_id`, `tp_order_id`) in `trading.positions`; the database CHECK forbids a flat
row from keeping any of them. Verify:

```sql
select symbol, qty, stop_loss, take_profit, stop_order_id, tp_order_id, leverage
from trading.positions;
```

A non-flat row with `stop_loss IS NULL` is **unprotected**: the monitor has nothing to
watch and nothing can be re-armed. Close it manually (`docs/runbooks/kill-switch.md`) or
re-open it with a stop rather than leaving it running.

The runner verifies every recorded stop against the venue (`GET /fapi/v1/openOrders`)
on **every reconciliation cycle** (`reconciler.position_interval_seconds`, default
60 s), and once immediately at startup before the first entry — a stop that is
cancelled or consumed mid-session is noticed within a cycle, not only after a restart:

- **`protection_intact`** — the id is still working; nothing is touched.
- **`protection_rearmed`** (WARNING alert) — the id is gone or was never recorded. Any
  leftover protection orders for the symbol are cancelled first (so the leg can never
  hold two stops), the exits are re-placed, the new ids are saved, and a
  `protection_rearmed` audit event is written. It is normal for the new id to differ
  from the old one.
- **`protection_check_failed`** / `unchecked` — the venue could not be asked. Nothing is
  cancelled or re-placed: an unknown state is not evidence the stop is gone.
- **`position_without_stop_level`** (CRITICAL alert) — nothing to re-arm; a human must
decide.

### A stop that filled partially is a remainder, not a closed position

`STOP_MARKET`/`TAKE_PROFIT_MARKET` orders are sent with `closePosition=true`, so they are
one-shot: the order disappears when it executes, even when the execution only covered
part of the quantity (a market order can be filled in several `userTrades`). The
signature is `stop_order_id` recorded, the id no longer in `openOrders`, and the position
still open — logged as `protection_stop_missing_with_open_position` and followed
immediately by a re-arm for the remaining quantity (WARNING alert,
`protection_rearmed`). The same path covers an operator cancelling the stop at the venue.

Alert repeats are suppressed per symbol and outcome: an unprotected position pages once,
not once a minute, while every cycle is still logged. A change of state always alerts.

### The venue can close before the local monitor does

Both the venue's stop and `apps/position_monitor` can close the same leg. Before an exit
is sent, the monitor asks the venue one question — how much of the leg it still holds
(`GET /fapi/v2/positionRisk`). When the venue holds **less** than the book, the exit is
withheld (`monitor_exit_skipped_venue_executed`): the book quantity is stale, and a
hedge-mode order cannot be trimmed by the venue (no `reduceOnly`), so closing the book's
quantity could be rejected or leave the book believing in exposure that is gone.

After the cycle's write transaction closes, one reconciliation pass books the venue's
fill (`venue_fill_reconciled`) and re-arms the remainder; the monitor never reconciles
inside its own transaction. An unreachable venue answers nothing and the exit proceeds;
the submission would fail anyway.

`/positions` shows the result per row: **🔒 Locked** when a venue stop id is recorded,
**🔓 Naked** when it is not. The badge reflects the *recorded* id; the per-cycle check is
what keeps it truthful. `aegis_positions_naked` and `aegis_entries_halted` expose both
states to Prometheus.
