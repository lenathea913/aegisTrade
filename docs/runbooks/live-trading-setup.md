# Live trading setup (paper → testnet → real money)

How to move AegisTrade from paper fills to Binance **Spot Testnet**, and then — only
after that is boring — to real money. Every stage is reversible; the last one is not.

## Ground rules

- **Never weaken a safety rule to make an operation succeed.** No setting, endpoint or
  UI button disables `RiskEngine`.
- **`AEGIS_LIVE_CONFIRM=true` opens a gate, it does not remove validation.**
  `ensure_testnet_base_url` still rejects lookalike hosts (`testnet.evil.com`,
  `api.testnet.evil.com`, …) and still requires `testnet.binance.vision` unless the
  confirmation flag is set. A typo in the URL cannot quietly reach mainnet.
- **Secrets stay in the environment.** `AEGIS_EXCHANGE__API_KEY` /
  `AEGIS_EXCHANGE__API_SECRET` are `SecretStr`; they are never logged, never stored in the
  database and never included in an alert.
- **Alerts never carry credentials.** The Telegram formatter drops any context key that
  looks like a token, secret or key.

## Stage 1 — Paper (default)

```bash
AEGIS_EXECUTION__MODE=paper
AEGIS_EXECUTION__ALLOW_LIVE_ORDERS=false
```

Nothing needs a key. The dashboard shows `PAPER MODE`; `POST /api/exchange/orders/...`
returns an empty list because there is no venue.

## Stage 2 — Binance Spot Testnet

1. Create a key at <https://testnet.binance.vision/> (no withdrawal permission).
2. Configure:

```bash
AEGIS_EXCHANGE__TESTNET=true
AEGIS_EXCHANGE__BASE_URL=https://testnet.binance.vision
AEGIS_EXCHANGE__API_KEY=<testnet key>
AEGIS_EXCHANGE__API_SECRET=<testnet secret>
AEGIS_EXECUTION__MODE=live            # live mode = "route to a venue"
AEGIS_EXECUTION__BROKER=binance-testnet
AEGIS_EXECUTION__ALLOW_LIVE_ORDERS=true
AEGIS_LIVE_CONFIRM=false              # still false: testnet does not need it
AEGIS_STREAMING__ENABLED=true
```

3. Start the stack (the streamer is part of it):

```bash
docker compose -f infra/compose/docker-compose.dev.yml up -d --build
```

4. Run the reconciler as a separate process; it is what records late fills:

```bash
docker compose -f infra/compose/docker-compose.dev.yml run --rm \
  -e AEGIS_EXECUTION__MODE=live -e AEGIS_EXECUTION__BROKER=binance-testnet \
  api python -m order_reconciler
```

5. Pre-flight checklist (never skippable once mode is live):

```bash
uv run python scripts/live_readiness_check.py
```

It checks: exchange ping, **signed credential check**, WebSocket feed (one real mark),
PostgreSQL, Redis, and the execution mode. In `live` mode it refuses to continue until
you type `I UNDERSTAND THE RISKS` exactly.

6. Verify in the dashboard:

- Header shows `TESTNET MODE` (amber) and `exchange connected`.
- `/exchange-orders` lists what the venue has open; `Cancel Order` asks for confirmation
  and is disabled while the venue is offline.
- Submit one minimum-size order and confirm: order row → `FILLED`, exactly one trade,
  one `TRADE_RECORDED` audit event.
- Alerts: `Order filled`, `Order rejected by exchange` / `by risk engine`,
  `WebSocket disconnected`, `Kill switch active`, `Daily loss limit reached`. Repeated
  failures are collapsed (2 s minimum gap, 60 s dedupe) — a broken venue does not spam.

### Failure drills (do these before trusting it)

| Drill | Do | Expect |
| --- | --- | --- |
| Exchange unreachable | Point the base URL at a closed port | Badge red, order list refuses, no order rows, no position |
| Bad credentials | Use a wrong secret | `ExchangeAuthError`, audit `ORDER_NOT_FILLED` (`reason=exchange_error`), ledger untouched |
| Stream dropped | `docker compose stop market_streamer` | `WebSocket disconnected` alert; marks expire after `mark_ttl_seconds`; the gate rejects stale quotes (no orders) |
| Killed mid-run | `kill -TERM <runner pid>` | Runner logs `runner_shutdown_requested` → `runner_stopped`, scheduler drains the job in flight |
| Restart after a fill | Stop the runner, fill an order manually, start it | Startup reconciliation logs `runner_startup_reconcile` and the trade appears once |

## Stage 3 — Real money (deliberate, slow)

Only when Stage 2 has run for a meaningful period with every drill passing.

1. **Rotate to live keys.** Create keys on <https://www.binance.com> with spot trading
   enabled, **no withdrawal permission**, and IP allow-listing if you have a fixed IP.
   Replace the two key values in the environment. Nothing else in the project changes.
2. **Point at the live endpoint and acknowledge it:**

```bash
AEGIS_EXCHANGE__BASE_URL=https://api.binance.com
AEGIS_EXCHANGE__TESTNET=false
AEGIS_LIVE_CONFIRM=true
```

   `AEGIS_LIVE_CONFIRM=true` is the *only* thing that lets the adapter accept a
   non-testnet host. It is read once at broker construction; leave it unset in every
   shared/dev environment.

3. **Tighten the limits first**, then the money:
   - `risk.max_risk_per_trade_pct` → a fraction of a percent,
   - `risk.max_daily_loss_pct` / `max_weekly_loss_pct` → what you are willing to lose,
   - `market_data.symbols` → one liquid symbol,
   - `execution.allow_live_orders=true` only for the process that trades.
4. **Run the checklist again.** It now prints `LIVE via binance-testnet`-style mode and
   still demands the typed confirmation:

```bash
uv run python scripts/live_readiness_check.py
```

   Do not proceed unless every line is `PASS` and the two warnings (streaming, alerting)
   are addressed.
5. **First live order: the smallest the venue allows.** Watch it in `/exchange-orders`,
   confirm the fill lands once in `trading.trades`, and confirm the alert arrives.
6. **Know how to stop.** `risk.kill_switch=true` (restart the runner) halts entries
   immediately and rejects every intent; the daily loss limit blocks new risk for the
   rest of the day. Both raise a critical alert.

## Rollback

```bash
AEGIS_EXECUTION__MODE=paper
AEGIS_EXECUTION__BROKER=paper
AEGIS_EXECUTION__ALLOW_LIVE_ORDERS=false
AEGIS_LIVE_CONFIRM=false
```

Restart the runner. Open orders stay on the venue: cancel them in `/exchange-orders`
first, then confirm the reconciler has no work left.

## What is still forbidden

- Real keys in a committed file, a shared environment, or a chat message.
- Setting `AEGIS_LIVE_CONFIRM=true` to "make the error go away".
- Any change that lets an order skip `ApprovedOrder`.
- Any UI affordance that disables the risk engine.
