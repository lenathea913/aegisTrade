# Runbook — backup and restore

**Use when:** before any migration or destructive operation, and on a schedule if the
platform holds history you care about.

PostgreSQL holds everything that matters: candles, orders, trades, positions and the
append-only audit log. Redis is cache/queue only and needs no backup.

## 1. Backup

```bash
mkdir -p backups
docker compose -f infra/compose/docker-compose.paper.yml exec -T postgres \
  pg_dump -U aegis -d aegis --format=custom --no-owner \
  > "backups/aegis-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

Verify the archive is readable before trusting it:

```bash
docker compose -f infra/compose/docker-compose.paper.yml exec -T postgres \
  pg_restore --list - < backups/aegis-<timestamp>.dump | head
```

Store backups outside the repository and outside the Docker volume. They contain the
full trading history; treat them as sensitive.

## 2. Restore (into an empty database)

Stop writers first so nothing lands mid-restore:

```bash
docker compose -f infra/compose/docker-compose.paper.yml stop api collector
```

Then restore:

```bash
docker compose -f infra/compose/docker-compose.paper.yml exec -T postgres \
  pg_restore -U aegis -d aegis --no-owner --clean --if-exists \
  < backups/aegis-<timestamp>.dump
```

Start the stack again and confirm migrations agree with the restored schema:

```bash
docker compose -f infra/compose/docker-compose.paper.yml up -d
make migrate                       # idempotent; applies anything newer than the dump
```

## 3. Verify after restore

```bash
docker compose -f infra/compose/docker-compose.paper.yml exec postgres \
  psql -U aegis -d aegis -c \
  "select (select count(*) from trading.trades)   as trades,
          (select count(*) from audit.audit_logs) as audit_rows,
          (select count(*) from market.candles)   as candles,
          (select version_num from alembic_version) as schema;"
```

The audit trigger is part of the schema, so it is restored with the tables. Confirm it
still refuses writes (see `docs/runbooks/database-migration.md`).

## 4. What a restore does not fix

A restore returns the database to a point in time. Anything that happened after that
point — including fills the exchange really made — is gone from the platform's view.
If the platform has ever traded real money, reconcile against exchange statements
before resuming (see `docs/runbooks/ledger-reconciliation.md`), and keep the kill
switch on until reconciliation is done.
