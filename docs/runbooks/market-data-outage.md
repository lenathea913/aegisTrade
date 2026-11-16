# Runbook — stale or missing market data

**Use when:** the collector is failing, candles stop arriving, or every intent is
rejected with `stale_market_data` / `missing_market_data`.

This is a safety feature, not a bug: with no trustworthy price, the risk engine
cannot measure risk, so it refuses to trade. The platform prefers standing still.

## 1. Confirm the symptom

```bash
curl -s localhost:8000/metrics | grep collector_lag        # seconds since last bar
curl -s localhost:8000/journal?limit=20 | jq '.entries[] | select(.title=="risk_rejected")'
make logs                                                  # look for "collector:" lines
```

Database view:

```bash
docker compose -f infra/compose/docker-compose.paper.yml exec postgres \
  psql -U aegis -d aegis -c \
  "select environment, symbol, timeframe, max(close_time) as newest,
          now() - max(close_time) as lag
   from market.candles group by 1, 2, 3 order by 1, 2;"
```

## 2. Diagnose

| Cause | Evidence | Fix |
| --- | --- | --- |
| CSV seed missing/misnamed | collector error `No such file` or `contains no candles` | restore `data/seeds/<BASE>_<timeframe>.csv`; `make seed-data` regenerates the synthetic one |
| Collector loop stopped | container restarted, no recent log lines | `docker compose ... restart collector` |
| Exchange API failing | `binance klines request failed: ...` with retries in the log | wait for the venue; the collector retries with backoff (`market_data.max_retries`) |
| Wrong symbol/timeframe configured | summary lists an unexpected series | fix `market_data.symbols` / `timeframes` and restart |
| Rows tagged with the wrong venue | `environment` column says `mainnet` while `exchange.testnet` is true | `make clean-db` and let the collector refill it |
| Clock skew on the host | negative lag, `stale_market_data` even after ingest | fix NTP; the staleness rule rejects future-dated quotes too |

The collector never invents candles: a failing series is reported in the run summary
and the rest of the pass continues.

## 3. Recover and verify

```bash
make collector-once       # one pass, prints JSON summary (inserted counts, lag)
```

Then confirm the risk engine agrees:

```bash
curl -s localhost:8000/metrics | grep collector_lag
```

And that trading is possible again — the next approved intent will show all nine risk
checks passed in `audit.audit_logs` (`order_approved` payload).

## 4. While data is stale

Do not disable the staleness rule or raise `stale_after_seconds` to "make it work".
Either restore the feed or stop trading deliberately with the kill switch
(`docs/runbooks/kill-switch.md`).
