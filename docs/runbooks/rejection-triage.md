# Runbook — triage a risk rejection

**Use when:** intents are being rejected and you need to know whether that is the
system protecting you or a configuration mistake.

Rejections are normal: the gate's job is to say no. What matters is which rule fired.
Every rejection appends one `risk_rejected` audit event whose `payload.reasons` holds
low-cardinality codes; the detail after `:` is for humans.

## 1. See why

```bash
curl -s "localhost:8000/journal?limit=50" | jq \
  '.entries[] | select(.title=="risk_rejected") | {at, symbol: .detail.entity_id, reasons: .detail.payload.reasons}'
```

Or in SQL:

```sql
select created_at, entity_id, payload->'reasons' as reasons
from audit.audit_logs
where event_type = 'risk_rejected'
order by created_at desc
limit 10;
```

## 2. Map the code to an action

| Reason | Meaning | Action |
| --- | --- | --- |
| `kill_switch_active` | Operator halt is on | Expected if you stopped trading; see `docs/runbooks/kill-switch.md` |
| `symbol_not_allowed` | Symbol not in `risk.limits.allowed_symbols` | Add it deliberately or fix the strategy |
| `missing_market_data` | No quote for the symbol | Check the collector (`docs/runbooks/market-data-outage.md`) |
| `stale_market_data` | Quote older than `market_data.stale_after_seconds` | Same runbook; do not raise the window to force a trade |
| `missing_entry_price` | MARKET intent with no quote and no reference price | Fix the data feed; the gate will not guess a price |
| `invalid_stop_for_side` | Stop on the wrong side of entry | Strategy bug — fix the signal, not the gate |
| `zero_stop_distance` | Stop equals entry | Strategy bug |
| `risk_per_trade_exceeded` | `|entry-stop| × qty` above the per-trade cap | Reduce size or widen the stop *deliberately* |
| `daily_loss_limit_reached` | Realized daily loss reached the floor | Stop for the day; this is the limit working |
| `weekly_loss_limit_reached` | Same, weekly | Stop for the week; review the strategy, not the limits |
| `max_total_exposure_exceeded` | Total exposure after this intent too high | Close something first or reduce size |
| `max_exposure_per_symbol_exceeded` | Per-symbol cap after this intent too high | Same |
| `max_leverage_exceeded` | Gross exposure / equity above `max_leverage` | Spot-only means 1×; reduce size |

## 3. Before changing any limit

1. Rejections are evidence, not noise. Count them by reason over a day:

   ```sql
   select payload->'reasons'->>0 as first_reason, count(*)
   from audit.audit_logs where event_type = 'risk_rejected'
   group by 1 order by 2 desc;
   ```

2. If a limit fires constantly at a size the strategy intends, the strategy is
   mis-sized, not the limit.
3. Changing a limit is a decision to accept more risk. Record the reason, change the
   YAML (version controlled), restart, and note it — the audit trail will show the new
   approvals from that moment on.
4. Never "fix" a rejection by bypassing `RiskGate`; nothing else can execute anyway
   (ADR 0003).
