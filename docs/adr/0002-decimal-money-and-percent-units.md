# ADR 0002 — Decimal money, percent units, frozen models

- **Status:** accepted
- **Date:** 2026-11-15

## Context

Two classes of bug are unacceptable in a ledger: silent precision loss and a limit
read as a fraction when it is a percentage (a 1 % limit applied as 100 %).
Floating point money and `0.01`-vs-`1` ambiguity are the classic sources of both.

## Decision

1. **Money and quantities are `Decimal` everywhere** (`NUMERIC(28,12)` in
   PostgreSQL). Floats appear only in descriptive statistics (`aegis_metrics`) and
   Prometheus gauges, where rounding is acceptable and documented.
2. **Percentages are percent units**: `1.0` means 1 %. Validated as `(0, 100]` for
   risk/loss caps, and `(0, 1000]` for exposure caps (futures may exceed 100 %).
   Every consumer must divide by 100 explicitly; there is no implicit fraction.
3. **Domain models are frozen** (`StrictModel`: frozen, `extra="forbid"`) and
   validated on construction. State changes return new instances via explicit
   methods, so an invalid transition raises instead of mutating history.
4. **Caps reject only when strictly exceeded; loss floors reject on reaching.**
   Touching the daily loss limit already means stop.

## Consequences

- A wrong unit is a validation error, not a silent 100× position.
- Serialisation must be explicit: the API sends money as strings, never floats.
- Boilerplate: every new field needs a type from `aegis_core.types` or a validator.

## Alternatives considered

- **Floats with rounding helpers.** Rejected: unrecoverable drift against an exchange.
- **Integer minor units (satoshis).** Rejected: crypto quote assets have varying
  decimals; `Decimal` + `NUMERIC` handles both without a fixed scale.
- **Mutable ORM-style entities.** Rejected: state transitions become untestable and
  audit reconstruction unreliable.
