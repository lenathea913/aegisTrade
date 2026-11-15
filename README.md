# AegisTrade

AegisTrade is a **personal, risk-first crypto trading platform**: research, paper
trading, execution through a mandatory risk gate, and a complete audit trail.

It is deliberately *not* a profit bot, not a price-prediction system, and it makes
**no claim about win rate or returns**. Its purpose is to make every trade
reproducible, every order checked against explicit risk limits, and every state
change explainable after the fact.

> Status: **FASE 10 — documentation complete**. Domain, schema, layered config, the
> eight-rule `aegis_risk` gate, the research stack, the idempotent
> `market_collector`, the audit-first `aegis_execution` / `aegis_ledger` path, the
> read-only FastAPI surface, the composed Postgres/Redis/Prometheus/Grafana stack,
> and the ADR + runbook set exist. Known gaps (no live adapter, no position monitor,
> no scheduled runner) are listed in `docs/architecture/overview.md`.

## Risk-first principles

1. **Strategies never touch an exchange.** A strategy emits an `OrderIntent` and
   nothing else.
2. **`RiskEngine` is the final gate.** Without an `ApprovedOrder`, no order can
   reach a broker. `ExecutionGateway` accepts `ApprovedOrder` only.
3. **`PortfolioLedger` is the source of truth** for PnL, positions, fees and
   funding. Nothing else computes money.
4. **Everything important is audited.** Significant events are appended to
   `audit_logs`; the table is append-only.
5. **Reproducibility over cleverness.** UTC everywhere (`timestamptz`), pinned
   configs per environment, deterministic backtests, no look-ahead.
6. **Paper first.** Live execution is a stub until risk, ledger and audit are
   proven in paper mode.

## Architecture (modular monolith)

```
              ┌──────────────────────────── apps ────────────────────────────┐
 market data  │ market_collector   research_worker   alerting   journal      │
   ──────────▶│       │                 │                │          │          │
              │       ▼                 ▼                │          │          │
              │  aegis_db         aegis_backtest         │          │          │
              │       │                 │                │          │          │
  strategy ──▶│  OrderIntent ──▶ RiskEngine ──▶ ApprovedOrder ──▶ ExecutionGateway
              │                       │                              │        │
              │                       ▼                              ▼        │
              │                  risk decision               PortfolioLedger  │
              │                       └──────────── audit_logs ────────────────┘
              └─────────────────────────────── api (FastAPI) ────────────────┘
```

One deployable monolith, one database, clear module boundaries. No microservices,
no Kafka, no Kubernetes in the MVP — see `docs/adr/0001-modular-monolith.md`.

## Repository layout

| Path | Purpose |
| --- | --- |
| `apps/api` | FastAPI REST surface for the dashboard (health, positions, risk budget) |
| `apps/market_collector` | Idempotent OHLCV collector worker |
| `apps/research_worker` | Backtests and walk-forward runs |
| `apps/risk_engine` | Thin service wrapper over `packages/aegis_risk` |
| `apps/alerting` | Optional Telegram/Slack notifications |
| `packages/aegis_core` | Domain types: `OrderIntent`, `ApprovedOrder`, `Position`, `Trade`, `RiskLimit` |
| `packages/aegis_execution` | `Broker` protocol, `PaperBroker`, `LiveBroker` stub, `ExecutionGateway`, `TradingPipeline` |
| `packages/aegis_ledger` | Positions, realized PnL windows, audit events, SQL persistence |
| `packages/aegis_strategy` | `StrategyBase` + one demo strategy |
| `packages/aegis_backtest` | Realistic backtest engine (fees, slippage, funding, latency) |
| `packages/aegis_risk` | Risk rules and `evaluate()` |
| `packages/aegis_metrics` | Expectancy, profit factor, drawdown, Sharpe, Sortino |
| `packages/aegis_db` | SQLAlchemy models + Alembic migrations |
| `packages/aegis_config` | `pydantic-settings` + YAML overlays per environment |
| `packages/aegis_observability` | Structured JSON logs with secret redaction + Prometheus metrics |
| `packages/aegis_alerting` | Alert value objects and a log-only transport (stub) |
| `configs/` | `base.yaml` + `dev` / `paper` / `live` overlays |
| `infra/` | Compose stacks, Prometheus scrape config, Grafana dashboards |
| `tests/` | unit, integration, fixtures |
| `docs/` | architecture, ADRs, runbooks |

## Requirements

- Python 3.11+ (3.13 works locally; CI pins 3.11)
- [uv](https://docs.astral.sh/uv/)
- `make` (Git Bash / WSL on Windows, or `choco install make`) — every target maps to a
  plain `uv run ...` command, so it is optional if you prefer running them directly
- Docker + Docker Compose (for Postgres/Redis and paper mode) — optional for unit tests

## Quickstart

```bash
make install       # uv sync --all-groups
cp .env.example .env   # fill in Postgres/Redis; exchange keys are optional in paper mode

make test          # whole suite
make test-unit     # unit tests only
make cov-core      # coverage gate for aegis_risk + aegis_backtest (>= 80%)

make lint          # ruff
make typecheck     # mypy --strict
make check         # lint + typecheck + tests
```

Run the API in development (Postgres/Redis via compose first):

```bash
make compose-dev   # postgres, redis, migrate, api, collector, prometheus, grafana
make dev           # uvicorn on http://localhost:8000  (GET /health, /positions, /risk/budget, /journal, /metrics)
```

Run the demo backtest (deterministic synthetic data, no network, no keys):

```bash
make seed-data       # regenerate data/seeds/BTCUSDT_1h.csv
make backtest-demo   # EMA cross over that data, with fees/slippage/latency
```

The printed statistics describe one synthetic run. They are not a forecast and
not a claim about live results.

Run **paper mode** — the full stack with a paper broker, no exchange keys, no real
orders:

```bash
make paper         # or: make compose-paper
make logs
make compose-down
```

## Configuration

Configuration is layered, highest precedence first:

1. environment variables, `AEGIS_<SECTION>__<FIELD>` (e.g. `AEGIS_RISK__KILL_SWITCH=true`)
2. `.env` (see `.env.example`)
3. `configs/<environment>.yaml`
4. `configs/base.yaml`

Every section is a frozen pydantic model with `extra="forbid"`, so a misspelled
key fails at startup instead of being silently ignored. Secrets are `SecretStr`
and are masked in logs, dumps and the API. Exchange keys are read from the
environment only — never from YAML, never written to disk.

```bash
# First-party packages live in packages/ (flat layout), so put them on PYTHONPATH
# when running Python directly. The Makefile exports this for every target.
PYTHONPATH=packages uv run python -c "from aegis_config import load_settings; print(load_settings('paper').execution.mode)"
```

## Database

PostgreSQL schemas: `market` (candles), `trading` (orders, positions, trades) and
`audit` (append-only `audit_logs`). Everything is `timestamptz` (UTC) and money is
`NUMERIC(28,12)` — never floating point.

```bash
make migrate                     # alembic upgrade head
make migration m="add x"         # autogenerate a revision
uv run alembic -c data/migrations/alembic.ini upgrade head --sql   # DDL without a DB
```

`audit.audit_logs` is append-only **in the database**: the initial migration
installs a trigger that rejects `UPDATE`, `DELETE` and `TRUNCATE`.

## Market data collector

`apps/market_collector` is the only writer of `market.candles`. One pass fetches the
latest closed bars for every configured symbol/timeframe and inserts them with
`ON CONFLICT (symbol, timeframe, open_time) DO NOTHING`, so re-runs, overlapping
windows and restarts can never duplicate or rewrite a bar.

```bash
make collector-once   # one pass, then exit
make collector-run    # loop every 60s (Ctrl-C to stop)
```

Data sources (`market_data.source` in the YAML):

- `csv` — paper/dev. Reads `data/seeds/<BASE>_<timeframe>.csv`, so the pipeline runs
  offline with no keys and no network.
- `exchange` — public Binance spot klines (`/api/v3/klines`). Read-only market data;
  no API key, no order endpoints, nothing that can trade.

Each request is wrapped in bounded exponential backoff
(`market_data.max_retries`, 1s → 2s → 4s … capped at 60s). A failing
symbol/timeframe is recorded in the run summary and the rest of the pass continues;
the collector never aborts silently or invents candles. `lag_seconds` (now minus the
newest stored close) is reported per series and becomes a Prometheus gauge later.

## Execution and ledger

One code path turns a strategy proposal into a recorded trade:

```
OrderIntent -> RiskGate.evaluate -> ApprovedOrder -> ExecutionGateway -> broker
            -> PortfolioLedger.apply_trade -> audit events (+ trading.* rows)
```

`TradingPipeline` is that path; nothing else wires the pieces together.

- **Approval is mandatory.** `ExecutionGateway.execute` accepts `ApprovedOrder`
  only, and checks at runtime too, so an `OrderIntent` (or anything untyped) raises
  `UnapprovedOrderError` instead of trading.
- **Paper broker.** Market fills pay adverse slippage; LIMIT orders fill only when
  the market reached the limit, at the limit price. Costs come from configuration
  (`execution.fee_bps`, `execution.slippage_bps`), never from hard-coded numbers.
- **Live is a stub.** `LiveBroker` refuses every call, and the gateway refuses to
  route to it unless `execution.allow_live_orders` is true. There is no live
  exchange adapter in the MVP and no withdrawal code anywhere.
- **The ledger is the source of truth.** Equity is
  `initial_equity + realized_pnl + unrealized_pnl(mark)`; realized PnL keeps every
  fee and funding event with its timestamp, so daily/weekly loss windows are
  recomputed rather than trusted from a counter. The risk gate always judges the
  ledger's frozen snapshot, never a live query.
- **Everything important is audited.** Rejections, approvals, submissions, fills,
  trades and position changes each append one `audit_logs` event; the table itself
  rejects `UPDATE`/`DELETE`/`TRUNCATE`.

```bash
uv run pytest tests/integration/test_end_to_end_order_flow.py -q
```

## API, logs and metrics

The API is **read-only**: no route places an order. Execution stays behind
`TradingPipeline` and is driven by the runner, not by HTTP.

| Endpoint | What it returns |
| --- | --- |
| `GET /health` | version, environment, execution mode, database reachability, kill switch |
| `GET /positions` | current exposure from the ledger, marked, money as strings |
| `GET /risk/budget` | equity, per-trade capacity, remaining daily/weekly loss budget, exposure and leverage vs limits |
| `GET /journal?limit=50` | executed trades and audit events merged newest-first |
| `GET /metrics` | Prometheus exposition (disable with `observability.metrics_enabled: false`) |

```bash
make dev     # uvicorn on http://localhost:8000, hot reload
curl -s localhost:8000/health | jq
curl -s localhost:8000/risk/budget | jq
```

A stopped PostgreSQL is reported as `status: "degraded"` with `database:
"unavailable"` rather than hanging: connections carry
`database.connect_timeout_seconds`. `/journal` answers `503` in that case, because
the journal genuinely needs the database; `/positions` and `/risk/budget` keep
working from the in-process ledger.

Logs are JSON by default (`observability.json_logs: false` for readable dev output)
and are passed through a redaction processor that masks any key matching
`secret|token|password|api_key|authorization` and every `SecretStr` value.

Alerting is a **stub**: `aegis_alerting` defines `Alert`, `Alerter`, `NullAlerter`
(disabled, the default) and `LoggingAlerter` (writes the alert to the structured
log). No Telegram/Slack network call is made anywhere in the MVP.

## Infra stack

One image (`infra/docker/Dockerfile`) runs every process; the command decides what
it is. Dependencies come from the committed lockfile, the container runs as a
non-root user, and `.env`/`.venv`/tests are excluded by `.dockerignore` so no secret
can be baked into a layer.

```bash
make docker-build        # build aegis-trade:dev
make compose-dev         # postgres + redis + migrate + api + collector + prometheus + grafana
make paper               # same stack pinned to configs/paper.yaml
make logs                # follow the paper stack
make compose-down
```

| Service | URL | Notes |
| --- | --- | --- |
| API | http://localhost:8000 | `/health`, `/positions`, `/risk/budget`, `/journal`, `/metrics` |
| Prometheus | http://localhost:9090 | scrapes `api:8000/metrics` every 15 s |
| Grafana | http://localhost:3000 | admin / `GRAFANA_ADMIN_PASSWORD`, dashboard "AegisTrade — equity, risk, market data" |
| Postgres | localhost:5432 | database `aegis`, migrations applied by the one-shot `migrate` service before the API starts |
| Redis | localhost:6379 | queue/cache only, never a source of truth |

The Grafana dashboard is provisioned from `infra/grafana/dashboards/` (equity, risk
decisions, rejections by reason, market-data lag, API requests by status) and only
uses metric names the application actually exports — the panels fail loudly rather
than showing invented numbers.

Stack variables (`POSTGRES_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`) live in the shell or
`.env`, which `docker compose` reads; the application itself never reads them. The
compose files ship development defaults only.

## Safety rules enforced by this repo

- No code path reaches an exchange without `RiskEngine` approval: the gateway only
  accepts `ApprovedOrder`, checks it at runtime, and an integration test proves a
  raw `OrderIntent` cannot execute.
- Secrets come from the environment only; nothing is written to logs or committed.
  `.env.example` contains placeholders.
- No machine learning or price prediction in this phase.
- No withdrawal API is implemented, ever.
- `live` mode stays inert until paper mode, ledger reconciliation and audit are proven.

## Documentation

- [`docs/architecture/overview.md`](docs/architecture/overview.md) — order path, module
  boundaries, data model, failure behaviour and known limitations
- [`docs/adr/`](docs/adr/README.md) — eight decisions: monolith, decimal money,
  risk-gate authority, append-only audit, pessimistic backtests, paper-first
  execution, observability, config/secrets
- [`docs/runbooks/`](docs/runbooks/README.md) — kill switch, paper stack, migrations,
  market-data outage, backup/restore, ledger reconciliation, rejection triage

## License

MIT — see `LICENSE`. Nothing here is financial advice; you are responsible for
any capital you expose to it.
