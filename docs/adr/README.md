# Architecture decision records

One file per decision, numbered, immutable once accepted. Superseding a decision means
writing a new ADR that references the old one — not editing history.

| ADR | Decision |
| --- | --- |
| [0001](0001-modular-monolith.md) | Modular monolith, not microservices |
| [0002](0002-decimal-money-and-percent-units.md) | Decimal money, percent units, frozen models |
| [0003](0003-risk-gate-as-final-authority.md) | The risk gate is the final authority |
| [0004](0004-postgres-schemas-and-append-only-audit.md) | PostgreSQL schemas and an append-only audit log |
| [0005](0005-backtest-realism-over-backtest-profit.md) | Backtests must be pessimistic, not flattering |
| [0006](0006-paper-first-and-a-live-broker-stub.md) | Paper first, live broker is a stub, no withdrawal path |
| [0007](0007-observability-json-logs-and-prometheus.md) | JSON logs with redaction, Prometheus metrics, alerting stub |
| [0008](0008-config-layering-and-secrets.md) | Layered config, strict validation, environment-only secrets |
| [0009](0009-protective-exits-through-the-gate.md) | Protective exits go through the gate, with a risk-reducing exemption |

## Format

```
# ADR NNNN — short title
- Status: proposed | accepted | superseded by ADR NNNN
- Date: YYYY-MM-DD
## Context
## Decision
## Consequences
## Alternatives considered
```

Keep ADRs short and specific: a future reader should be able to tell what was decided,
why, and what it cost.
