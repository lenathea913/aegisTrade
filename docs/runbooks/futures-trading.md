# Futures trading (USDT-M, Testnet only)

AegisTrade started as a spot platform. This runbook covers the futures mode: what it
does, what it refuses to do, and how to run it without discovering the limits in
production.

**Status: testnet only.** Futures requests are refused for mainnet venues and for
`execution.mode: live` — by configuration and again by the adapter. Do not expect a flags
combination that unlocks it; adding mainnet is a code change plus a manual verification
pass, deliberately.

## What futures adds

| Concept | Spot | Futures (this platform) |
| --- | --- | --- |
| Market type | `spot` | `futures` (USDT-M perpetual) |
| Margin | full notional | **isolated**, `notional / leverage` |
| Leverage | 1x | 1x–5x, chosen per trade |
| Liquidation | n/a | estimated; stop must be hit first |
| Funding | n/a | simulated in paper mode (1 bp per 8 h) |
| Shorts | only by selling holdings | first-class `positionSide: SHORT` |
| Venue host | `testnet.binance.vision` | `testnet.binancefuture.com` |

## Configuration

```bash
AEGIS_EXECUTION__MARKET_TYPE=futures        # spot (default) | futures
AEGIS_EXECUTION__MAX_LEVERAGE=5             # 1..5, refused above 5
AEGIS_EXECUTION__BROKER=binance-testnet     # real testnet orders (paper otherwise)
AEGIS_EXECUTION__ALLOW_LIVE_ORDERS=true     # still required for any testnet order
AEGIS_EXCHANGE__TESTNET=true                # futures refuses mainnet
AEGIS_EXCHANGE__FUTURES_BASE_URL=https://testnet.binancefuture.com
```

`risk.limits.max_leverage` (in the YAML overlay) is the **gate's** cap and must be set
too — the effective ceiling is the smaller of it and the hard 5. For a futures overlay:

```yaml
execution:
  market_type: futures
  max_leverage: 5
risk:
  limits:
    max_leverage: 5
```

Leverage above 5 is rejected at three levels: `ExecutionSettings.max_leverage` (`le=5`),
`RiskLimit.max_leverage` (validator), and `OrderIntent.leverage` (`le=MAX_LEVERAGE`).

## How leverage is chosen

It is **not** configured per trade. Sizing is fixed-fractional:

```
qty        = (equity * risk_per_trade_pct / 100) / |entry - stop|
notional   = qty * entry
leverage   = ceil(notional / equity)          # clamped to [1, min(config, 5)]
qty        = min(qty_by_risk, equity * leverage * 0.95 / entry)
```

Consequences worth internalising:

* a **wide stop** (high volatility) sizes small and needs 1x;
* a **tight stop** sizes large and needs more leverage, up to 5x;
* the 0.95 factor leaves room for fees and slippage before the fill;
* the risk at the stop stays the same fraction of equity either way — leverage is a
  *funding* decision here, not a risk multiplier.

The gate then re-checks the result: `leverage_cap_exceeded` if the intent asks for more
than the cap, `insufficient_leverage` if the notional does not fit the declared margin,
and `liquidation_too_close` if the stop would be reached *after* the liquidation price.

## Execution path

```
EmaCrossStrategy -> Signal -> SignalService.convert (sizes qty + leverage)
    -> TradingPipeline -> RiskGate -> ExecutionGateway
    -> BinanceFuturesTestnetBroker
         POST /fapi/v1/marginType   {"marginType": "ISOLATED"}   (once per symbol)
         POST /fapi/v1/leverage     {"leverage": N}              (when N changes)
         POST /fapi/v1/order        side + positionSide + quantity
```

`positionSide` is carried by the intent: closing a long is a **SELL on the LONG leg**, so
the adapter never guesses it from the side. `reduceOnly` is not sent — Binance rejects it
in hedge mode.

Margin mode is isolated, always: `set_margin_type("CROSSED")` raises, and `-4046`
("No need to change margin type") is treated as success rather than an error.

## Ledger accounting

* `margin_used = notional / leverage` (isolated).
* Unrealized PnL is **independent of leverage** on a linear contract; ROE is not:
  `roe_pct = unrealized / margin * 100`.
* Liquidation estimate: `entry * (1 -/+ 1/leverage +/- 0.005)`. Display and sanity
  check only — the venue's number is authoritative.
* Paper funding: every 8 hours, `notional * paper_funding_bps / 10000`, longs pay and
  shorts receive. Charged by the runner's monitor cycle; only whole intervals are charged,
  so a 1-minute cadence debits exactly as often as an 8-hour one.

## Protection and reconciliation

Two rules keep a leveraged position safe when the process is not:

* **The stop is persisted and also lives on the venue.** After a fill the approved
  `stop_loss`/`take_profit` are written to `trading.positions`, so a restart re-arms the
  monitor, and the futures adapter places reduce-only-style `STOP_MARKET` /
  `TAKE_PROFIT_MARKET` orders with `closePosition=true` and `workingType=MARK_PRICE`.
  When the leg closes those orders are cancelled (only protection order types — a queued
  entry is never touched). If the venue refuses the stop, the fill stands, the alert is
  CRITICAL and an audit row `protection_failed` is written.
* **The book is compared with the venue every 60 s.** Any difference in net quantity,
  entry (> 0.5 %) or leverage halts new entries (the protective monitor keeps running)
  and turns the header badge red. See `docs/runbooks/ledger-reconciliation.md`.

## Backtesting leverage

`BacktestConfig` carries `leverage` (1-5) and `margin_type` (`ISOLATED` only). The
strategy's quantity is read as the margin committed at 1x, so at 3x the same collateral
carries three times the notional: PnL, fees and funding scale with leverage, and the
simulated position is force-closed with `exit_reason="liquidated"` at
`entry * (1 -/+ 1/leverage +/- 0.005)` once a bar trades through that price. A stop that
sits *between* the entry and the liquidation price still fills first; a stop beyond it
never gets the chance. At leverage 1 no liquidation is modelled (spot-equivalent).

```bash
PYTHONPATH="packages;apps" uv run python scripts/run_futures_backtest.py
PYTHONPATH="packages;apps" uv run python scripts/run_futures_backtest.py --from-db --leverage 1 --leverage 3
```

### Collecting a year of history

The collector only fetches the latest page (1 000 bars), which keeps a live series
current but cannot feed a multi-month backtest. `scripts/backfill_candles.py` pages
backwards through the same klines format, anchoring each request one millisecond before
the oldest bar already received, and writes through the collector's idempotent upsert:

```bash
# 10 000 hourly bars (~14 months) of the USDT-M contract, from the futures testnet host
PYTHONPATH="packages;apps" uv run python scripts/backfill_candles.py --target 10500
```

The default host is `testnet.binancefuture.com` (`/fapi/v1/klines`) because the spot
testnet keeps only a few weeks of history and the futures contract is what the backtest
trades. Re-running is free: existing bars are skipped, never duplicated.

The script labels what it writes: `--venue futures` by default for a `/fapi` path, and
`market.candles.venue` is part of the series key. The collector writes futures bars to
the futures series too (it follows `execution.market_type` for both the host and the
label), so the two markets no longer share a row — verify with:

```bash
psql -U aegis -d aegis -c "select venue, symbol, timeframe, count(*), max(close_time)
  from market.candles group by 1, 2, 3 order by 1, 4 desc;"
```

`BacktestResult.liquidations` (alias `total_liquidations`) counts the forced exits, so a
run tells you directly whether the stop was ever reached before the collateral.

### Regime filter and mean reversion

An EMA cross is a trend follower, and in a market that is only oscillating every cross
is noise. `EmaCrossParams.adx_threshold` (default **25**, period 14) makes the strategy
ignore a cross while ADX is below it; `None` disables the filter, which is what a
baseline run needs. The default pair is **50/200** — the pair that lost the least over a
year of history.

`BollingerReversionStrategy` bets the other way: it buys when price crosses into the
lower band and sells when it crosses into the upper one, on the theory that a stretch
away from the mean is more often faded than continued.

```bash
PYTHONPATH="packages;apps" uv run python scripts/run_futures_backtest.py --from-db \
  --leverage 1 --ema 9/21 --ema-adx 9/21 --ema 50/200 --bollinger
```

`--ema` runs a pair unfiltered, `--ema-adx` adds the ADX floor, `--bollinger` adds the
mean-reversion strategy. All rows share the same bars, quantity and costs, so the only
thing that differs between them is the strategy.

## Operating it

```bash
# 1. Migrate (adds leverage, stop_loss/take_profit and trading.reconciliation_state)
PYTHONPATH="packages;apps" uv run alembic -c data/migrations/alembic.ini upgrade head

# 2. Paper futures: leverage + margin + funding, no keys needed
AEGIS_EXECUTION__MARKET_TYPE=futures \
AEGIS_RISK__LIMITS__MAX_LEVERAGE=5 \
PYTHONPATH="packages;apps" uv run python -m trading_runner --once

# 3. Testnet futures: needs futures testnet keys
AEGIS_EXECUTION__MARKET_TYPE=futures \
AEGIS_EXECUTION__BROKER=binance-testnet \
AEGIS_EXECUTION__ALLOW_LIVE_ORDERS=true \
PYTHONPATH="packages;apps" uv run python -m trading_runner
```

Dashboard: the header shows a `FUTURES` badge, and `/positions` gains `Leverage`,
`Margin`, `Liq. Price` and `Unrealized (ROE%)` columns. Spot positions still render,
with leverage `1x` and empty margin/liq cells.

## Shutting it down

1. Set `AEGIS_RISK__KILL_SWITCH=true` (stops entries; exits still run).
2. Close open positions from `/positions` (the manual close goes through the gate) or let
   the monitor hit its stop/target.
3. Unset `AEGIS_EXECUTION__MARKET_TYPE` to return to spot.

## Known limits

* Futures is **not** wired to mainnet, and the adapter refuses `fapi.binance.com`.
* The liquidation price is an estimate that ignores fee tiers and maintenance-margin
  brackets; it is used for display and for the "stop beyond liquidation" check only.
* ONE-WAY mode is not supported: the adapter always sends hedge-mode `positionSide`.
* Funding in paper mode uses one constant rate; the venue's rate varies and is settled
  from the mark price.
* The position comparison covers symbols the book knows plus every leg the venue
  reports; it does not reconcile *orders* older than the ones still open.
* A hedge pair that nets to zero counts as flat on both sides, not as a drift.
