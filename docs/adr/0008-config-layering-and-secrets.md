# ADR 0008 — Layered config, strict validation, environment-only secrets

- **Status:** accepted
- **Date:** 2026-11-15

## Context

Configuration mistakes in a trading system are indistinguishable from code bugs: a
shadowed YAML key silently restores a default limit, and a typo in an env var quietly
does nothing. Secrets in files get committed.

## Decision

1. **Precedence, highest first:** `AEGIS_*` environment variables → `.env` →
   `configs/<environment>.yaml` → `configs/base.yaml` → code defaults. Environment
   always wins so a container can override a checked-in file.
2. **Nested env naming:** `AEGIS_<SECTION>__<FIELD>`, e.g.
   `AEGIS_RISK__KILL_SWITCH=true`.
3. **`extra="forbid"` everywhere.** An unknown key is an error at startup, not a
   silently ignored line.
4. **Secrets are `SecretStr`, read from the environment only**, masked in logs and in
   `redacted_dump()`. `.env` is gitignored and excluded from the Docker build context;
   `.env.example` holds placeholders only.
5. **Cross-field validation lives in the model**, not in call sites: live mode
   requires credentials, `execution.broker` must match `execution.mode`, alerting
   channels must be complete when enabled.
6. **One settings object is built at process start** and frozen; a running process
   does not silently pick up a changed YAML file.

## Consequences

**Positive**

- A typo fails fast and loudly at boot; a limit can no longer be silently stale.
- Secrets never appear in a repository, an image layer or a log line.

**Negative**

- Changing `risk.kill_switch` requires restarting the process (or calling the gate
  with the flag explicitly), which is why the kill-switch runbook restarts the API.
- Every new setting needs a field and, where relevant, a validation rule.

## Alternatives considered

- **Environment-only configuration.** Rejected: limits deserve review and version
  control; YAML overlays document intent per environment.
- **Dynamic config reload.** Deferred: reload adds failure modes (partial config,
  half-applied limits) that are worse than a restart for a personal platform.
- **HashiCorp Vault / cloud secret managers.** Rejected for the MVP: a file-based
  `.env` on one machine, excluded from git and images, is proportionate.
