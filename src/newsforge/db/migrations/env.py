"""Alembic environment for NewsForge (SQLite + PostgreSQL).

The URL is resolved at runtime — never committed — from the application config:
NEWSFORGE_DATABASE_URL (canonical DSN) -> NEWSFORGE_DB_PATH (legacy) -> the
default SQLite path (see newsforge.config). PostgreSQL ALWAYS enforces foreign
keys; SQLite keeps its historical per-connection PRAGMA behaviour, so the two
execution modes only differ in DDL shape (batch_alter_table is used for SQLite).
"""
from __future__ import annotations

import sys
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the package importable when Alembic runs from a checkout (CLI + runtime).
_SRC = Path(__file__).resolve().parents[3]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from newsforge.config import DatabaseConfig  # noqa: E402
from newsforge.db.base import Base  # noqa: E402
from newsforge.db import models as _models  # noqa: E402,F401  # register every table

# NOTE: alembic.ini has no logging handlers ON PURPOSE. When migrations run
# embedded (init_db/_alembic_upgrade) they must inherit the application's
# structured logging — fileConfig() would rewire the ROOT logger and silently
# suppress application INFO lines (regression surfaced by test_observability).
config = context.config
target_metadata = Base.metadata


def _config_url() -> str:
    cfg = DatabaseConfig()
    raw = str(cfg.path)
    if raw.startswith(("postgresql", "postgres", "mysql", "mariadb", "sqlite")):
        return raw
    return f"sqlite:///{Path(raw).as_posix()}"


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url") or _config_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    if not config.get_main_option("sqlalchemy.url"):
        config.set_main_option("sqlalchemy.url", _config_url())
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()