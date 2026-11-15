# ADR 0006 — Paper first, live broker is a stub, no withdrawal path

- **Status:** accepted
- **Date:** 2026-11-15

## Context

This is personal infrastructure handling real money on a machine with no
co-located execution, no 24/7 monitoring and no exchange adapter yet. A live-trading
bug is expensive and irreversible; a paper-trading bug is a lesson.

## Decision

1. **`PaperBroker` is the only implemented venue.** It fills MARKET orders at the
   current price plus adverse slippage and charges fees in basis points from
   configuration; LIMIT orders fill only when the market reached the limit.
2. **`LiveBroker` exists as an explicit stub that raises.** It never sends anything.
3. **Two independent switches**: `execution.mode` must be `live` *and*
   `execution.allow_live_orders: true`, and live mode additionally requires exchange
   credentials at config load. The gateway refuses a live broker unless the second
   switch is on.
4. **No withdrawal capability exists anywhere** in the codebase — not disabled code,
   absent code. Market data access is public and read-only.
5. **Exchange credentials are optional in paper mode** and read only from the
   environment as `SecretStr`, masked by `redact()` before any log record is written.

## Consequences

**Positive**

- Every risk/ledger/audit defect surfaces in paper mode with fake money.
- A leaked repository contains no key material and no code path that can move funds.

**Negative**

- The platform cannot trade for real until an adapter is written, reviewed and
  tested against a testnet — deliberate friction.
- Paper fills are optimistic about liquidity: they assume the full quantity fills at
  one price.

## Alternatives considered

- **Real exchange adapter behind a feature flag.** Rejected for the MVP: an untested
  adapter behind a flag is one typo away from live orders.
- **A generic CCXT integration.** Deferred: large surface, and market-data needs are
  already met by the public klines endpoint.
