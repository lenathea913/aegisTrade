# Runbook — stop all trading (kill switch)

**Use when:** the strategy behaves unexpectedly, data looks wrong, the ledger does
not reconcile, or you simply want trading to stop now. The kill switch rejects every
intent before a broker is reached.

## 1. Turn it on

Local process:

```bash
export AEGIS_RISK__KILL_SWITCH=true
make dev          # restart so the frozen settings object picks it up
```

Compose stack:

```bash
# infra/compose/docker-compose.paper.yml sets AEGIS_RISK__KILL_SWITCH: "false".
# Override it for this deployment, then restart the API:
AEGIS_RISK__KILL_SWITCH=true docker compose \
  -f infra/compose/docker-compose.paper.yml up -d api
```

## 2. Confirm it took effect

```bash
curl -s localhost:8000/health | jq '.kill_switch'          # must be true
curl -s localhost:8000/risk/budget | jq '.kill_switch'     # must be true
```

Any new intent now produces a `risk_rejected` audit event with
`kill_switch_active` and no order row.

```bash
docker compose -f infra/compose/docker-compose.paper.yml exec postgres \
  psql -U aegis -d aegis -c \
  "select created_at, entity_id, payload->'reasons' from audit.audit_logs
   where event_type = 'risk_rejected' order by created_at desc limit 5;"
```

## 3. What the kill switch does **not** do

- It does not close open positions **automatically** — and it deliberately stops the
  position monitor from doing so, because a halt means "stop touching the market".
  While the switch is on, protect the book by hand (or accept the exposure) and
  expect every exit attempt to appear as a `risk_rejected` event with
  `kill_switch_active`.
- It does not stop the collector (market data keeps flowing, which is intentional:
  you want data when you investigate).
- It does not stop the position monitor; the monitor keeps running and its exits are
  rejected, which is visible in its log and in `/journal`.
- It does not stop the API from serving reports.

## 4. Turn it back off

Only after the cause is understood and written down (see the incident note in
`docs/runbooks/rejection-triage.md` and the audit query above):

```bash
unset AEGIS_RISK__KILL_SWITCH      # or set it to false in the compose override
make dev
curl -s localhost:8000/health | jq '.kill_switch'   # must be false
```

Removing the switch is a decision, not a formality: record why, and what changed.
