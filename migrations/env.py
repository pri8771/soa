"""Alembic environment.

The database URL resolves from ``SOA_DATABASE_URL`` (async driver URLs are
converted to their sync equivalent for migration execution). Migrations are
applied only through explicit operator commands — application processes
never invoke this module.
"""

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from soa_db.base import Base

config = context.config

DEFAULT_URL = "postgresql+asyncpg://soa_dev:soa_dev_password@localhost:5432/soa"


def _database_url() -> str:
    url = os.environ.get("SOA_DATABASE_URL", DEFAULT_URL)
    # Alembic runs synchronously; swap async drivers for sync ones.
    return url.replace("+asyncpg", "+psycopg").replace("+aiosqlite", "")


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
