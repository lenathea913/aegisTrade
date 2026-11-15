# Runbook — database migrations

**Use when:** applying schema changes to a running deployment, or checking what a
migration would do before touching data.

Migrations are Alembic revisions in `data/migrations/versions/`. The initial revision
is hand-written and installs the three schemas, the tables, the indexes, the CHECK
constraints and the append-only audit triggers.

## 1. Review before applying

Render the SQL without a database (safe anywhere):

```bash
uv run alembic -c data/migrations/alembic.ini upgrade head --sql
```

Check that the output contains, for the current head, `CREATE SCHEMA market/trading/
audit`, the tables you expect, and `CREATE TRIGGER ... audit_logs`.

## 2. Apply

Local:

```bash
make migrate      # alembic upgrade head against AEGIS_DATABASE__URL
```

Compose: the one-shot `migrate` service runs the same command before `api` starts:

```bash
docker compose -f infra/compose/docker-compose.paper.yml run --rm migrate
```

## 3. Verify

```bash
docker compose -f infra/compose/docker-compose.paper.yml exec postgres \
  psql -U aegis -d aegis -c "select version_num from alembic_version;"

docker compose -f infra/compose/docker-compose.paper.yml exec postgres \
  psql -U aegis -d aegis -c "\dt trading.*" -c "\dt audit.*" -c "\dt market.*"
```

Confirm the audit guards are in place (must raise an error):

```sql
update audit.audit_logs set actor = 'tampered' where id = 1;   -- expect exception
delete from audit.audit_logs where id = 1;                     -- expect exception
```

## 4. Generating a new revision

```bash
make migration m="add order_intents"
```

Review the generated file **before** committing: hand-write the parts Alembic cannot
infer (data backfills, trigger creation, CHECK constraints with stable names) and keep
`upgrade()`/`downgrade()` symmetric.

## 5. Rolling back

```bash
uv run alembic -c data/migrations/alembic.ini downgrade -1
```

Rolling back drops schema objects, including `audit_logs` and its contents. Take a
backup first (see `docs/runbooks/backup-restore.md`); a downgrade is not a way to
"undo" a bad trade, and it destroys audit history.

## Guardrails

- Never edit an applied migration; add a new revision.
- Never drop the audit triggers to make an update pass.
- Keep enum-like columns as `VARCHAR` + `CHECK` (ADR 0004) so widening a set stays a
  plain migration.
