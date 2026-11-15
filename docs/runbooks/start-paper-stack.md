# Runbook — start, verify and stop the paper stack

**Use when:** bringing the platform up locally for paper trading, a demo, or to
verify a change end to end.

Requirements: Docker with Compose v2. No exchange credentials are needed; paper mode
reads committed CSV seeds and never places a real order.

## 1. Prepare configuration

```bash
cp .env.example .env      # placeholders only; .env is gitignored
```

Set at least these in `.env` (or export them):

```
POSTGRES_PASSWORD=<strong password>
GRAFANA_ADMIN_PASSWORD=<strong password>
```

`AEGIS_ENVIRONMENT` is already `paper` inside the paper compose file; do not override
it with `live` unless you have credentials **and** have reviewed ADR 0006.

## 2. Start

```bash
docker compose -f infra/compose/docker-compose.paper.yml config   # validate
make paper                                                        # build + up -d
make logs                                                         # follow
```

Order of startup is enforced by the compose file: `postgres` (healthy) → `migrate`
(one-shot `alembic upgrade head`) → `api` and `collector`.

## 3. Verify

```bash
curl -s localhost:8000/health | jq
# status must be "ok" and database "ok"; "degraded" means Postgres is unreachable

curl -s localhost:8000/risk/budget | jq '.equity, .limits'
curl -s localhost:8000/positions | jq '.count'
curl -s localhost:8000/metrics | grep aegis_ | head
```

Grafana: <http://localhost:3000> → folder **AegisTrade** → dashboard
"AegisTrade — equity, risk, market data". Prometheus target
`aegis-api` must be `UP` at <http://localhost:9090/targets>.

Market data check (the collector should have written the seed bars):

```bash
docker compose -f infra/compose/docker-compose.paper.yml exec postgres \
  psql -U aegis -d aegis -c \
  "select symbol, timeframe, count(*), max(close_time) from market.candles
   group by 1, 2;"
```

## 4. Stop

```bash
make compose-down        # stops both stacks, keeps volumes
```

Data lives in the `pgdata` volume; removing it (and with it the ledger and the
audit trail) is a deliberate `docker compose down -v`, not part of routine shutdown.

## Troubleshooting

| Symptom | First check |
| --- | --- |
| `api` exits immediately | `make logs`; usually a missing `POSTGRES_PASSWORD` or an invalid config key (`extra="forbid"`) |
| `/health` degraded | `docker compose ... ps | grep postgres`, then the API log for the DSN |
| `/journal` 503 | same as above: the journal needs the database |
| No candles | collector log; then `AEGIS_MARKET_DATA__CSV_DIR` and that `data/seeds/BTCUSDT_1h.csv` exists in the image |
| Grafana has no data | Prometheus target `UP`? dashboard time range? |
