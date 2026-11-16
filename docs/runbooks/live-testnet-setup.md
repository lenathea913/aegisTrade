# Live Testnet setup

How to run AegisTrade against **Binance Spot Testnet** — real API, real order
lifecycle, fake money. Use it to prove the adapter, reconciler and UI badges before
anyone discusses real money. Real keys are out of scope for this project.

## Ground rules

- **Testnet keys only.** Keys from `testnet.binance.vision`. A live-key trade is a
  production incident, not a config choice.
- **The adapter refuses non-testnet hosts.** `AEGIS_EXCHANGE__BASE_URL` must resolve to
  `testnet.binance.vision`; anything else raises `NonTestnetEndpointError` unless the
  operator sets `AEGIS_LIVE_CONFIRM=true`. Leave that `false`.
- **The risk gate is never bypassed.** No setting, endpoint or UI button turns
  `RiskEngine` off. Testnet changes the *venue*, not the checks.
- **Secrets stay in the environment.** `AEGIS_EXCHANGE__API_KEY` /
  `AEGIS_EXCHANGE__API_SECRET` are read as `SecretStr`, never written to the database,
  never logged (the logging layer redacts them).

## 1. Get testnet credentials

1. Open <https://testnet.binance.vision/>, log in with GitHub, and create an API key.
2. Enable spot trading permission on that key. No withdrawal permission is needed.
3. Put both values in your local, git-ignored `.env` (never in a committed file):

```bash
AEGIS_EXCHANGE__BASE_URL=https://testnet.binance.vision
AEGIS_EXCHANGE__TESTNET=true
AEGIS_EXCHANGE__API_KEY=<testnet key>
AEGIS_EXCHANGE__API_SECRET=<testnet secret>
AEGIS_LIVE_CONFIRM=false
AEGIS_STREAMING__ENABLED=true
```

## 2. Select the testnet broker

The execution section decides routing:

```bash
AEGIS_EXECUTION__MODE=live
AEGIS_EXECUTION__BROKER=binance-testnet
```

`build_broker` then returns `BinanceTestnetBroker`. With `mode=paper` nothing changes:
the paper broker keeps filling locally and the dashboard badge stays `PAPER`.

## 3. Start the stack

```bash
docker compose -f infra/compose/docker-compose.dev.yml up -d --build
docker compose -f infra/compose/docker-compose.dev.yml ps
```

`market_streamer` is part of the stack and writes marks to
`aegis:mark:<symbol>` in Redis. The reconciler is a separate process; run it with:

```bash
docker compose -f infra/compose/docker-compose.dev.yml run --rm \
  -e AEGIS_EXECUTION__MODE=live -e AEGIS_EXECUTION__BROKER=binance-testnet \
  api python -m order_reconciler
```

It polls every `reconciler.interval_seconds` (default 5) and records late fills,
partial fills and cancellations. It never approves an order.

## 4. Verify before trusting it

1. **Badge.** `http://localhost:8000/` shows `TESTNET MODE`, an amber chip, and an
   `exchange connected` pill. Red/rose means the venue is unreachable — **stop here**.
2. **Ping.** `curl -s http://localhost:8000/api/exchange/status` returns
   `"connected": true` and `"label": "TESTNET"`.
3. **Marks.** `docker compose ... exec redis redis-cli --scan --pattern 'aegis:mark:*'`
   lists the configured symbols; `get aegis:mark:BTC/USDT` has a recent `observed_at`.
4. **One tiny order.** Let the runner place one minimum-size testnet order and confirm:
   - the order row moves `NEW`/`SUBMITTED` → `FILLED` within a few reconciler passes;
   - exactly one trade exists per fill (no double counting);
   - an audit row `TRADE_RECORDED` names `actor=order_reconciler` when the reconciler
     recorded it.

## 5. Failure drills (do these, they are the point)

| Drill | Do | Expect |
| --- | --- | --- |
| Exchange unreachable | Point `AEGIS_EXCHANGE__BASE_URL` at a closed port, restart API | Badge turns red, Convert disabled, no new order rows |
| Bad credentials | Use a wrong secret | `ExchangeAuthError`; pipeline writes `ORDER_NOT_FILLED` with `reason=exchange_error`; ledger untouched |
| Timeout | Block the host / drop packets | Same safe failure path; no position appears |
| Streamer stopped | `docker compose stop market_streamer` | Marks expire after `mark_ttl_seconds`; the monitor falls back to closed candles |

In every drill the invariant is the same: **a failed exchange call leaves no position
and no trade behind.**

## 6. Shut down

```bash
docker compose -f infra/compose/docker-compose.dev.yml down
```

Then rotate the testnet key in the Binance testnet UI. Keys are cheap; a leaked one is
an inconvenience, not a loss, but rotate anyway.

## What is still forbidden

- Real API keys, real endpoints, `AEGIS_LIVE_CONFIRM=true` in a shared environment.
- Any change that lets an order skip `ApprovedOrder`.
- Any UI affordance that disables the risk engine on testnet.
