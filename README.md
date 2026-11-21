# AegisTrade

AegisTrade is a **personal, risk-first crypto trading platform**: research, paper
trading, execution through a mandatory risk gate, and a complete audit trail.

It is deliberately *not* a profit bot, not a price-prediction system, and it makes
**no claim about win rate or returns**. Its purpose is to make every trade
reproducible, every order checked against explicit risk limits, and every state
change explainable after the fact.

> Status: **FASE 13 — real-time marks and a testnet adapter**. On top of the paper
> stack (risk gate, research, collector, audit/ledger, position monitor, dashboard)
> AegisTrade now streams marks over WebSocket, ships a **Binance Spot Testnet**
> broker that refuses non-testnet hosts, reconciles open orders with the exchange
> every five seconds, and shows mode + connectivity badges in the header. The
> dashboard still cannot execute anything: conversion stops at a `DRAFT` intent, and
> Convert is disabled whenever the exchange is unreachable. Testnet only — never
> real keys; see `docs/runbooks/live-trading-setup.md`.
> Schema, triggers, idempotency, the journal and ledger reconstruction were
> exercised on PostgreSQL 16; the Docker image is built in CI, not on the build
> machine (no Docker locally).

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
| `apps/market_streamer` | Binance WebSocket marks (`bookTicker` mid price) into Redis |
| `apps/order_reconciler` | Reconciles open orders with the exchange; records late fills |
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
`ON CONFLICT (environment, venue, symbol, timeframe, open_time) DO NOTHING`, so re-runs,
overlapping windows and restarts can never duplicate or rewrite a bar. The
`environment` column (`testnet`/`mainnet`) is part of that key: testnet prices can
never be read back as mainnet history. So is `venue` (`spot`/`futures`): a spot kline
and a USDT-M perpetual kline for the same minute are different prices, so they are
different series and every reader filters on `execution.market_type`. `make clean-db`
empties the table so a fresh collector pass refills it from a single venue.

```bash
make collector-once   # one pass, then exit
make collector-run    # loop every 60s (Ctrl-C to stop)
```

Data sources (`market_data.source` in the YAML, env `AEGIS_MARKET_DATA__SOURCE`):

- `binance` (the default) and `exchange` — public Binance spot klines
  (`/api/v3/klines`). Read-only market data; no API key, no order endpoints, nothing
  that can trade. The host follows `exchange.testnet`: `testnet.binance.vision` while
  true, `api.binance.com` when false.
- `csv` — offline seeds in `data/seeds/<BASE>_<timeframe>.csv`, for deterministic
  backtests and CI. Not the default any more: paper mode rehearses the live feed.
- Any other value fails at startup instead of silently falling back to CSV.

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

Exits use the same path: `apps/position_monitor` compares the latest closed candle
with each position's recorded stop/target and sends a closing intent, which the gate
accepts as risk-reducing (caps for *new* risk are skipped; the kill switch, the
allow-list, data freshness and stop validity are not). Run it with
`make monitor-once` or `make monitor-run`.

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

No route places an order. Execution stays behind `TradingPipeline` and is driven
by the runner, not by HTTP; the signal endpoints can only produce a **`DRAFT`
intent**, and conversion re-runs the risk engine (400 with the reasons if refused).

| Endpoint | What it returns |
| --- | --- |
| `GET /health` | version, environment, execution mode, database reachability, kill switch |
| `GET /positions` | current exposure from the ledger, marked, money as strings |
| `GET /risk/budget` | equity, per-trade capacity, remaining daily/weekly loss budget, exposure and leverage vs limits |
| `GET /journal?limit=50` | executed trades and audit events merged newest-first |
| `GET /metrics` | Prometheus exposition (disable with `observability.metrics_enabled: false`) |
| `GET /motivation` | one line from `TRADER_MOTIVATION_QUOTE` (behaviour, not a signal) |
| `POST /api/signals/generate` | scan stored candles once, store the signals it finds |
| `GET /api/signals`, `/api/signals/{id}` | signals with status, levels and risk preview |
| `POST /api/signals/{id}/convert` | draft intent + fresh `RiskGate.evaluate`; `400` if refused |
| `POST /api/signals/{id}/ignore` | retire a signal without converting it |
| `GET /`, `/positions`, `/journal`, `/signals` | HTML dashboard when the client sends `Accept: text/html` |

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

`/positions` and `/journal` answer HTML to a browser and JSON to everything else,
so the API contract survives the dashboard without a second set of paths.

## Web dashboard

`apps/api/templates/` holds Jinja2 templates (Tailwind via CDN, dark by default,
HTMX for polling) and `apps/api/static/app.css` the few rules Tailwind cannot
express. Overview, Positions and Signals poll every 5 seconds; converting a signal
swaps only the detail panel. There is no Node build step and no React/Vue/Next.

The dashboard is deliberately narrow: it shows health, budget, exposure, PnL,
positions, journal and signals, and it can turn a signal into a **draft**. It
cannot approve, execute or cancel anything.

## Ledger reconstruction

`PortfolioLedger` still computes everything in memory, but on startup the API calls
`reconstruct_from_db(session)`: it replays `trading.trades` in fill order with the
same accounting used live, then compares the result with `trading.positions`. A
disagreement raises `LedgerError` (strict by default) rather than serving a
plausible-looking lie. See `docs/adr/0010-ledger-reconstruction-signals-dashboard.md`.

Logs are JSON by default (`observability.json_logs: false` for readable dev output)
and are passed through a redaction processor that masks any key matching
`secret|token|password|api_key|authorization` and every `SecretStr` value.

Alerting is a **stub**: `aegis_alerting` defines `Alert`, `Alerter`, `NullAlerter`
(disabled, the default) and `LoggingAlerter` (writes the alert to the structured
log). No Telegram/Slack network call is made anywhere in the MVP.

## Autonomous runner and API token

FASE 12 adds the two things a "passive" platform lacks: a daemon that trades on a
schedule, and a lock on the HTTP surface.

**API token.** `AEGIS_API_TOKEN` protects every route except `/health` and `/metrics`:
`/api/*`, the dashboard pages, the HTMX partials and the UI mutation routes. A browser
can log in with HTTP Basic (any username, the token as the password) and the API sets
an `aegis_token` cookie so HTMX keeps working; scripts send
`Authorization: Bearer <token>`. An empty token is development mode: requests pass and
the API logs `api_auth_disabled` at startup. There is no read/write split — a page that
can convert a signal needs the token exactly like `POST /api/signals/{id}/convert`.

**Runner.** `apps/trading_runner` runs two APScheduler jobs in one process: entries
every `runner.signal_interval_seconds` (default 300 s) and protective exits every
`runner.monitor_interval_seconds` (default 60 s). A cycle checks the kill switch,
refuses stale candles, generates signals, converts the `RISK_OK` ones and sends the
draft intent to `TradingPipeline`. It never builds an `ApprovedOrder` itself, and it
refuses to start when `execution.mode` is `live` while `execution.allow_live_orders` is
false.

| Command | What it does |
| --- | --- |
| `make runner-once` | one entry scan + one monitor pass, JSON summary, then exit |
| `make runner-start` | daemon on `configs/dev.yaml` |
| `make runner-paper` | daemon on `configs/paper.yaml` |

`position_monitor.use_live_price: true` makes the monitor act on the ledger's latest
mark instead of waiting for a closed candle (default `false`, the paper-safe choice).
Every tick writes its order, trade, position and audit rows in one transaction, so a
protective exit appears in `trading.trades` and in `/journal` immediately.
Details: [`docs/runbooks/autonomous-trading.md`](docs/runbooks/autonomous-trading.md).

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
- The autonomous runner never approves its own orders: it produces a draft intent
  and `TradingPipeline` runs `RiskGate` before any broker is constructed.
- No withdrawal API is implemented, ever.
- `live` mode stays inert until paper mode, ledger reconciliation and audit are proven.

## Documentation

- [`docs/architecture/overview.md`](docs/architecture/overview.md) — order path, module
  boundaries, data model, failure behaviour and known limitations
- [`docs/adr/`](docs/adr/README.md) — nine decisions: monolith, decimal money,
  risk-gate authority, append-only audit, pessimistic backtests, paper-first
  execution, observability, config/secrets, protective exits
- [`docs/runbooks/`](docs/runbooks/README.md) — kill switch, paper stack, migrations,
  market-data outage, backup/restore, ledger reconciliation, rejection triage,
  autonomous trading runner

## License

MIT — see `LICENSE`. Nothing here is financial advice; you are responsible for
any capital you expose to it.
