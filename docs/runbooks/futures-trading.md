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

## Operating it

```bash
# 1. Migrate (adds trading.positions.leverage / trading.trades.leverage)
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
* Reconciliation reads `positionRisk` only when called; the reconciler still works on
  orders, not on venue positions.
