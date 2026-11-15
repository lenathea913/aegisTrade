"""Alembic environment.

The database URL comes from ``aegis_config`` (env ``AEGIS_DATABASE__URL``), never
from this file, so credentials are not duplicated into the repository.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path
from typing import Any

from alembic import context
from sqlalchemy import create_engine, pool, text

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (REPO_ROOT / "packages", REPO_ROOT / "apps"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from aegis_config import load_settings  # noqa: E402
from aegis_db import Base  # noqa: E402
import aegis_db.models  # noqa: E402,F401  (registers every table on Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_settings = load_settings()
_database_url = _settings.database.url
config.set_main_option("sqlalchemy.url", _database_url.replace("%", "%%"))

target_metadata = Base.metadata

#: Schemas owned by this application. Created before any migration runs so the
#: first revision and autogenerate both see a consistent namespace.
SCHEMAS: tuple[str, ...] = ("market", "trading", "audit")


def _configure(**kwargs: Any) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_schemas=True,
        compare_type=True,
        compare_server_default=True,
        version_table_schema="public",
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Render SQL without connecting (used by CI to lint the DDL)."""
    _configure(url=_database_url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url, poolclass=pool.NullPool, future=True)
    with engine.connect() as connection:
        for schema in SCHEMAS:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        connection.commit()

        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
