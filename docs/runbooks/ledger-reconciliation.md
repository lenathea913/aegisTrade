# Runbook — reconcile the ledger with the database

**Use when:** `/positions` or `/risk/budget` looks wrong, the platform was restarted,
a restore happened, or you suspect the in-process ledger and the tables disagree.

The ledger is the source of truth **while a process runs**; the tables are the durable
record. Restoring ledger state from the database is not implemented yet, so after a
restart the ledger starts from `risk.initial_equity` with no positions — that is the
most common source of a "mismatch".

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

## 4. Known gap

Automatic reconciliation (load positions and realized PnL on startup, compare with the
tables, refuse to start on mismatch) is **not implemented**. Until it is, treat any
restart as untrusted for `/positions` and run the checks above before resuming.
