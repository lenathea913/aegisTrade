# AegisTrade architecture

AegisTrade is a **risk-first personal crypto trading platform**: it exists to make
risk visible, decisions reproducible and history auditable. It is not a profit bot,
it does not predict prices, and no document here claims a win rate.

The MVP is one deployable monolith: one database, one image, clear module
boundaries. No microservices, no Kafka, no Kubernetes.

## The order path

Everything that can move money passes through exactly one path:

```
OrderIntent            strategy output: what it wants to do (never executable)
    |
    v  RiskGate.evaluate(intent, portfolio_state, market_context)
RiskDecision           approved with evidence, or rejected with every violated rule
    |
    v  ApprovedOrder
ExecutionGateway       only accepts ApprovedOrder; runtime check backs the type
    |
    v  Broker             PaperBroker fills; LiveBroker is a stub that refuses
Trade                  one executed fill, with fee and funding
    |
    v  PortfolioLedger    positions, realized PnL windows, equity, exit levels
   +-> audit_logs         append-only trail of every step
```

Exits take the same path. `apps/position_monitor` watches the positions the ledger
holds, compares the latest closed candle with the recorded stop and target, and sends a
closing intent when a level is crossed. The gate recognises such an intent as
**risk-reducing** and skips the caps that limit new risk (risk-per-trade, loss floors,
exposure, leverage) while still enforcing the kill switch, the allow-list, data
freshness and stop validity — see `docs/adr/0009-protective-exits-through-the-gate.md`.

`TradingPipeline` (`packages/aegis_execution/pipeline.py`) is the only composition
of those steps. Services call the pipeline instead of wiring gate, gateway and
ledger themselves, which is what makes "no order bypasses risk" a testable property
rather than a convention (`tests/integration/test_end_to_end_order_flow.py`).

## Modules

| Package | Responsibility | Must not |
| --- | --- | --- |
| `aegis_core` | Frozen domain: `OrderIntent`, `ApprovedOrder`, `Order`, `Position`, `Trade`, `Candle`, `RiskLimit`, `PortfolioState` | depend on I/O or frameworks |
| `aegis_config` | Typed settings: YAML overlays + `AEGIS_*` env, `extra=forbid`, `SecretStr` | store secrets in files |
| `aegis_db` | SQLAlchemy models (`market`, `trading`, `audit`), engine/session plumbing | contain business rules |
| `aegis_risk` | Eight rules and `RiskGate`; returns evidence for approvals and all reasons for rejections | execute anything |
| `aegis_strategy` | `StrategyBase` and the demo EMA cross; emits `OrderIntent` only | read the ledger or place orders |
| `aegis_backtest` | Bar-by-bar simulation with fees, slippage, funding, latency | claim live performance |
| `aegis_metrics` | Descriptive statistics (expectancy, PF, drawdown, Sharpe, Sortino) | predict prices |
| `aegis_execution` | Broker protocol, `PaperBroker`, `LiveBroker` stub, `ExecutionGateway`, `TradingPipeline` | accept an unapproved intent |
| `aegis_ledger` | Positions, realized PnL history, marks, equity snapshot, audit events, SQL persistence | read market data on its own |
| `aegis_observability` | JSON logs with redaction, Prometheus metrics | log secrets |
| `aegis_alerting` | Alert value objects and a log-only transport (stub) | call the network in the MVP |
| `apps/market_collector` | Idempotent OHLCV ingest into `market.candles` | trade |
| `apps/position_monitor` | Closes protected positions when their stop or target is touched, through the gate | bypass the gate or call an exchange |
| `apps/api` | Read-only HTTP surface over the ledger and journal | place orders |

## Data model

- `market.candles` — primary key `(symbol, timeframe, open_time)`, `ON CONFLICT DO
  NOTHING` on ingest, so re-runs can never duplicate a bar.
- `trading.orders` — the submitted order **plus the approval evidence** (approval id,
  risk checks, max risk amount, stop, take profit), so an order can be explained
  after limits change.
- `trading.trades` — one row per fill, with fees and funding.
- `trading.positions` — current state per symbol, upserted by the ledger.
- `audit.audit_logs` — append-only; a database trigger rejects `UPDATE`, `DELETE`
  and `TRUNCATE`.

Money is `NUMERIC(28,12)` and `Decimal` end to end; timestamps are `timestamptz`
stored in UTC; enum-like columns are `VARCHAR` + `CHECK` so growing an enum is a
plain migration.

## Consistency and failure behaviour

- **The ledger is the source of truth while a process runs.** Risk always judges a
  frozen `PortfolioState` snapshot taken from it, never a live query mid-decision.
- **A collector failure is isolated** per symbol/timeframe and reported in the run
  summary; retries use bounded exponential backoff.
- **A missing or stale quote blocks trading** (`missing_market_data`,
  `stale_market_data`); the platform prefers doing nothing to guessing.
- **A database outage degrades the API** (`/health` → `degraded`, `/journal` → 503)
  instead of hanging: database connections carry a connect timeout.
- **The kill switch is an argument bound into `RiskGate`**, so a caller cannot
  forget it; with it on, every intent is rejected before any broker is touched.

## Known limitations (deliberate, tracked)

- No live exchange adapter: `LiveBroker` refuses everything, and there is no
  withdrawal code anywhere.
- No scheduled **entry** runner yet: the API never executes, and the paper compose
  stack runs collector + API, not the position monitor. Entries currently require a
  caller that composes `TradingPipeline`.
- The monitor acts on closed candles, not ticks, so a stop is enforced when a bar
  shows the level was crossed.
- The monitor restores open positions and their newest stop/target from the tables at
  startup, but realized PnL history is not reconstructed, so its loss windows restart
  at zero. The API's ledger is not restored at all.
- The API has no authentication; it is meant for localhost.
- The schema, triggers and persistence paths were verified against a real PostgreSQL
  16 instance (migration, append-only triggers, `ON CONFLICT` idempotency, JSONB and
  `timestamptz` round-trips, live `/health` and `/journal`). What has **not** been run
  here is the Docker image and the compose stack: this machine has no Docker.

## Further reading

- `docs/adr/` for the decisions behind the above.
- `docs/runbooks/` for what to do when something breaks.
