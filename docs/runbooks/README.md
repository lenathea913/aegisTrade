# Runbooks

Operational procedures for the single-operator deployment. Each runbook is written to
be followed under pressure: a symptom, a diagnosis, exact commands, and what *not* to
do.

| Runbook | Use when |
| --- | --- |
| [kill-switch.md](kill-switch.md) | Trading must stop now, or resume after a halt |
| [start-paper-stack.md](start-paper-stack.md) | Bringing the paper stack up and verifying it |
| [database-migration.md](database-migration.md) | Applying, reviewing or rolling back schema changes |
| [market-data-outage.md](market-data-outage.md) | Candles are stale or missing and intents are rejected |
| [backup-restore.md](backup-restore.md) | Before destructive work, and to recover |
| [ledger-reconciliation.md](ledger-reconciliation.md) | Positions or equity look wrong after a restart |
| [rejection-triage.md](rejection-triage.md) | Risk rejections need explaining |

## Principles

- **Never weaken a safety rule to make an operation succeed.** If data is stale, stop
  trading; do not stretch the staleness window.
- **Never rewrite history.** `audit.audit_logs` is append-only in the database; correct
  forward with a new, recorded action.
- **Back up before you touch schema or data.**
- **Record the decision.** Every halt, limit change and repair needs a reason written
  down next to what was done.
