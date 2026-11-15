# ADR 0007 — JSON logs with redaction, Prometheus metrics, alerting as a stub

- **Status:** accepted
- **Date:** 2026-11-15

## Context

A single operator needs to answer three questions quickly: what happened, how much
risk is left, and is the data fresh. Logs must never leak credentials, and dashboards
must not show numbers the system does not actually produce.

## Decision

1. **Structured JSON logs** via `structlog`, one object per line, with a redaction
   processor that masks any key matching `secret|token|password|api_key|authorization`
   and every `SecretStr` value *before* rendering.
2. **Prometheus counters and gauges** in `aegis_observability.Metrics`, each instance
   owning its registry (no duplicate-registration surprises in tests). Labels are
   low-cardinality: rejection reasons are truncated at `:` so
   `max_total_exposure_exceeded:12000>10000` becomes one label value.
3. **`GET /metrics` on the API**, gated by `observability.metrics_enabled`.
4. **Grafana dashboards are provisioned from version-controlled JSON**, read-only in
   the UI, and may only reference metric names the application exports.
5. **Alerting ships as a stub.** `NullAlerter` (default) discards; `LoggingAlerter`
   writes one structured record. No Telegram/Slack network call exists in the MVP.

## Consequences

**Positive**

- Dashboards cannot drift from reality: a missing metric shows "No data", not a
  fabricated line.
- A pasted log line is safe to share.

**Negative**

- Metrics live in the API process only; the collector's lag is a gauge that something
  must set — currently nothing does, so that panel shows no data until the collector
  exports its own metrics.
- Log-only alerts require someone to read the logs; real paging is future work.

## Alternatives considered

- **OpenTelemetry traces.** Deferred: high value for distributed systems, low value
  for one process.
- **`prometheus_client` global registry.** Rejected: tests instantiate several apps
  and would crash on duplicate metric registration.
- **Logging raw exception objects.** Rejected: stack traces can embed request data,
  including headers with credentials.
