# Runbook: autonomous trading runner

The runner (`apps/trading_runner`) is the only process that turns signals into orders
without a human click. It runs two schedules on one `BlockingScheduler`:

| Job | Default cadence | What it does |
| --- | --- | --- |
| `entries` | every 300 s (`runner.signal_interval_seconds`) | generate signals, convert `RISK_OK` ones, send them to `TradingPipeline` |
| `monitor` | every 60 s (`runner.monitor_interval_seconds`) | check protected positions and send protective exits through the same pipeline |

Both jobs share a lock and the same ledger instance. Neither can create an
`ApprovedOrder`: they build a draft `OrderIntent` and `TradingPipeline` runs
`RiskGate`.

## Start it

```bash
# one pass, prints a JSON summary (useful before trusting the daemon)
make runner-once

# paper mode against configs/paper.yaml (recommended first)
make runner-paper

# dev config
make runner-start
```

Directly:

```bash
PYTHONPATH="packages;apps" uv run python -m trading_runner
PYTHONPATH="packages;apps" uv run python -m trading_runner --once
PYTHONPATH="packages;apps" uv run python -m trading_runner --no-monitor
```

Environment overrides (they beat the YAML):

```bash
AEGIS_RUNNER__SIGNAL_INTERVAL_SECONDS=120 AEGIS_RUNNER__MONITOR_INTERVAL_SECONDS=30 \
AEGIS_ENVIRONMENT=paper uv run python -m trading_runner
```

### Startup safety checks

- `execution.mode: live` with `execution.allow_live_orders: false` → the runner
  **refuses to start** (`RunnerConfigError`). There is no flag to skip this check.
- The ledger is rebuilt from `trading.trades` before the first tick, so risk sees the
  real book rather than a fresh `initial_equity`.
- With `AEGIS_API_TOKEN` unset the API logs `api_auth_disabled` at startup; the runner
  itself does not expose HTTP.

## Stop it

```bash
# graceful: SIGINT/SIGTERM (Ctrl-C in the foreground)
# the daemon logs `runner_stopped` and exits without touching open positions
```

To halt **trading** without stopping the process, use the kill switch instead:

```bash
AEGIS_RISK__KILL_SWITCH=true make runner-start   # or set it and restart
```

A cycle with the kill switch on logs `runner_kill_switch` and does no entry work at
all. The monitor still runs, but the gate rejects every intent while the switch is on
(`kill_switch_active`), so protective exits are blocked too — that is deliberate: the
kill switch means "no orders", not "no orders except mine". See
[`kill-switch.md`](kill-switch.md).

## Watch it

```bash
# readable dev logs
AEGIS_OBSERVABILITY__JSON_LOGS=false PYTHONPATH="packages;apps" uv run python -m trading_runner

# in Docker / compose
docker compose -f infra/compose/docker-compose.dev.yml logs -f api
```

Key log events:

| Event | Meaning |
| --- | --- |
| `runner_started` | schedule, mode, kill-switch state |
| `runner_cycle` | one scan: how many signals generated, how many candidates |
| `runner_kill_switch` | kill switch on, entries skipped |
| `runner_candle_missing` / `runner_candle_stale` | no fresh data; **no entry** |
| `runner_signal_converted` | a draft intent was produced for a signal |
| `runner_order_filled` | the pipeline filled it (trade id in the payload) |
| `runner_order_rejected` | the gate refused; payload carries the reasons and a motivation quote |
| `runner_monitor_action` / `runner_monitor_rejected` | protective-exit attempt and its verdict |

Every rejection also writes an `audit.audit_logs` row (`risk_rejected`), which is what
the dashboard journal shows.

## What it will not do

- It never builds `ApprovedOrder` itself and never calls a broker directly.
- It never trades on a stale candle: entries are skipped per symbol when
  `market_data.stale_after_seconds` is exceeded.
- It never converts a signal twice: `SignalService.convert` marks the signal
  `CONVERTED`, so a filled or rejected conversion is not retried.
- It does not persist the signal store: signals live in process memory, so a restart
  loses pending ideas (their orders and trades are already in the database).
- It does not run the collector. Without fresh candles, every cycle logs
  `runner_candle_stale` and does nothing.

## Persistence

Each tick uses one database session: the order row, the trade row, the position
upsert and the audit events commit together. A protective exit therefore appears in
`trading.trades` (and in `/journal`) in the same commit that records the audit trail.
