# ADR 0010 — Reconstruct the ledger, and keep the dashboard honest

- **Status:** accepted
- **Date:** 2026-11-15

## Context

Two gaps made the system look more finished than it was.

First, `PortfolioLedger` was pure in-memory state. The pipeline persisted every trade
and the resulting position, but a restart began from `initial_equity` with no
positions and no realized PnL. `/positions` then reported a flat book while
`trading.positions` said otherwise — a plausible-looking lie, which is worse than an
error.

Second, everything the system knew was reachable only through JSON endpoints. Seeing
whether the bot was healthy, what the risk budget was, or why an order was refused
meant reading logs. A dashboard that computes its own numbers would have become a
second source of truth; one that can trigger execution would have bypassed the
pipeline.

## Decision

1. **Trades are the record; positions are a cache.** `reconstruct_from_db` replays
   `trading.trades` in `(filled_at, trade_id)` order through the same
   `Position.apply_fill` accounting the live path uses, including fees and funding at
   the moment of realization. Daily and weekly windows are then derived from the
   replay relative to the current UTC instant, never from a stored counter.
2. **Disagreement is an error, not a warning.** After replaying, the result is
   compared with `trading.positions`. With `strict=True` (the default) a mismatch
   raises `LedgerError`; `strict=False` returns the summary for inspection. Serving
   numbers that contradict the durable rows is not allowed.
3. **Startup is best-effort but loud.** `apps/api/main.py` asks the service to
   reconstruct during lifespan startup when a real session factory exists. A failure
   is logged as `ledger_reconstruction_failed` and the API still starts: a dashboard
   that cannot restore state should say so, not refuse to boot. The failure is
   visible, and `/health` keeps reporting database reachability.
4. **Signals are domain objects with a hard expiry.** `Signal` is immutable
   (pydantic), carries its levels, `reason_codes`, `market_context` and a mandatory
   `expires_at`, and moves `NEW → RISK_OK | BLOCKED_BY_RISK → CONVERTED | EXPIRED |
   IGNORED`. Generation is a dry run: `RiskGate.evaluate` decides the initial status.
5. **Conversion never trusts the preview.** `POST /api/signals/{id}/convert` sizes a
   draft intent from the per-trade risk capacity, then runs `RiskGate.evaluate`
   again. A refusal returns `400` with the reasons and marks the signal blocked. The
   result is a `DRAFT` `OrderIntent`; execution remains the pipeline's job.
6. **The dashboard reads the service, not the database.** Jinja2 templates plus HTMX
   (Tailwind via CDN, dark by default) render the same `ApiService` data as the JSON
   routes. `/positions` and `/journal` choose HTML or JSON from `Accept`, so the
   dashboard needed no parallel API. There is no Node build step, no SPA, and no route
   that approves or executes an order.
7. **Motivation is behaviour, not prediction.** `TRADER_MOTIVATION_QUOTE` is a fixed
   list of process reminders; `get_random_motivation()` picks one and
   `TradingPipeline` logs it (INFO) when an order is refused, so a rejection carries a
   next action instead of only a reason code.

## Consequences

- A restart restores exposure and realized windows before the first request; a
  corrupted or hand-edited positions table fails loudly.
- Replay cost grows with trade count. Acceptable at personal-trading scale; if a book
  ever holds millions of fills, replay should be windowed or checkpointed.
- In-memory signal storage means signals do not survive a restart. They are ideas
  with an expiry, not records; the audit trail keeps the decisions that mattered.
- The UI cannot place trades, which is a deliberate limit: a dashboard bug can
  mislead a human, but it cannot move money.
