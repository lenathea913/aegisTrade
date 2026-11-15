# ADR 0001 — Modular monolith, not microservices

- **Status:** accepted
- **Date:** 2026-11-15

## Context

AegisTrade is built and operated by one person on one machine. The domain has a
small number of hard invariants (risk approval, ledger truth, append-only audit)
that are easy to violate across process boundaries and hard to test if every
component talks over a network.

The usual argument for microservices — independent scaling and team autonomy —
does not apply: there is one operator, one strategy, one database, and no traffic to
scale.

## Decision

Ship one deployable monolith with strict internal module boundaries:

- first-party packages under `packages/`, entry points under `apps/`;
- one PostgreSQL database, one Redis (queue/cache only);
- one Docker image running every process with a different command;
- no Kafka, no Kubernetes, no service mesh, no cross-service RPC.

Boundaries are enforced by types and tests, not by the network: `OrderIntent`
cannot execute, only `ApprovedOrder` can, and `TradingPipeline` is the sole
composition of gate → gateway → ledger.

## Consequences

**Positive**

- One transaction can cover order + trade + position + audit row.
- End-to-end tests run in-process in milliseconds.
- One image and one compose file to operate, back up and reason about.

**Negative**

- Scaling means scaling the whole process; acceptable for a personal platform.
- Modules could drift into each other without discipline, so the import direction is
  documented in `docs/architecture/overview.md` and reviewed in pull requests.

## Alternatives considered

- **Microservices per entity.** Rejected: distributed transactions for a single-owner
  ledger; unbounded operational cost for zero benefit.
- **Serverless functions per endpoint.** Rejected: cold starts and no long-lived
  ledger/collector loop; the risk path benefits from a single process holding state.
