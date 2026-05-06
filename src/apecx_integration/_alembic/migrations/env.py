"""Alembic env — imports Base.metadata from the Control Plane ORM layer.

The DB URL is read from the ``APECX_CP_DB_URL`` environment variable if set,
otherwise falls back to ``sqlalchemy.url`` in alembic.ini (SQLite default).
This lets the same migration run against SQLite (laptop default) and Postgres
(AC7 parity tests) without editing alembic.ini.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from apecx_integration.control_plane.models import entities  # noqa: F401 — register mappers
from apecx_integration.control_plane.models.base import Base
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False preserves loggers already configured by
    # pytest or the application. The default (True) would set disabled=True on
    # every logger not listed in alembic.ini, silently swallowing WARNING
    # records from apecx_integration loggers for the rest of the test session.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

if db_url := os.environ.get("APECX_CP_DB_URL"):
    config.set_main_option("sqlalchemy.url", db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url is not None and url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
