"""Minimal schema-versioning + startup validation (an Alembic-free *boundary*).

Design constraints (OPS_HARDENING_PERSISTENCE):
- ``create_all`` is kept as the table-creation mechanism; it is safe to run on every
  startup because SQLAlchemy only creates tables that do not exist yet (it never alters
  or drops existing tables). The risk it does NOT cover is *schema drift*: an existing
  table that gained a column in the models is silently left unchanged, so queries fail
  at runtime instead of at boot.
- This module closes that gap with two explicit, fail-fast checks:

  1. **schema_version** — a ``_newsforge_meta`` row records the schema version the DB
     was created/migrated with. If the stored version is LOWER than the build's
     ``SCHEMA_VERSION`` a migration is required (refuse to start with a clear message).
     If it is HIGHER the DB was written by a newer build (refuse — never downgrade).
  2. **column drift** — every table currently declared in ``Base.metadata`` must expose
     all of its declared columns on SQLite (``PRAGMA table_info``). A missing column
     raises :class:`SchemaIncompatibleError` before the app serves traffic.

This is a *boundary*, not a migration tool: when the first real migration is needed the
next phase (OPS_HARDENING_MIGRATIONS) decides Alembic; until then the boundary keeps
schema changes explicit instead of silent.

Backwards compatibility: pre-existing NewsForge databases
(created by earlier ``create_all`` runs, without the meta table) are *adopted*: the meta
table is created and stamped with the current version on first start. No data is touched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError, OperationalError

from newsforge.db.base import Base

# Bump ONLY via an explicit migration in the next phase. Creating/changing a column
# without bumping this defeats the boundary.
SCHEMA_VERSION = 1
_META_TABLE = "_newsforge_meta"
_META_KEYS = ("schema_version", "applied_at", "newsforge_build")


class SchemaIncompatibleError(RuntimeError):
    """The on-disk schema version differs from what this build supports."""


def _create_meta_table(engine) -> None:
    ddl = (
        f"CREATE TABLE IF NOT EXISTS {_META_TABLE} "
        f"(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(ddl)


def _read_meta(engine) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(f"SELECT key, value FROM {_META_TABLE}").fetchall()
    return {str(k): str(v) for k, v in rows}


def _write_meta(engine, values: dict[str, str]) -> None:
    with engine.begin() as conn:
        for key, value in values.items():
            try:
                conn.exec_driver_sql(
                    f"INSERT INTO {_META_TABLE} (key, value) VALUES (?, ?)",
                    (key, value),
                )
            except IntegrityError:
                # A concurrent first-boot already stamped it; that is fine in the
                # single-writer MVP. (Re-raise after commit handler? No — safe ignore.)
                pass


def ensure_schema_version(engine, *, expected: int = SCHEMA_VERSION) -> int:
    """Stamp a fresh DB or validate an existing one's version; return the stored version."""
    _create_meta_table(engine)
    meta = _read_meta(engine)
    stored_raw = meta.get("schema_version")

    if stored_raw is None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write_meta(engine, {
            "schema_version": str(expected),
            "applied_at": now,
        })
        return expected

    try:
        stored = int(stored_raw)
    except (TypeError, ValueError):
        raise SchemaIncompatibleError(
            f"corrupt schema_version marker in {_META_TABLE}: {stored_raw!r}"
        ) from None

    if stored > expected:
        raise SchemaIncompatibleError(
            f"database schema_version={stored} is NEWER than this build supports "
            f"({expected}); refusing to start against a newer/downgraded codebase."
        )
    if stored < expected:
        raise SchemaIncompatibleError(
            f"database schema_version={stored} is older than this build ({expected}); "
            f"a migration is required before startup (schema boundary)."
        )
    return stored


def _missing_declared_columns(engine) -> dict[str, list[str]]:
    """Tables whose on-disk columns do not cover the current model's declarations.

    Only meaningful for SQLite (uses PRAGMA); non-SQLite engines are skipped so the
    ORM stays portable (PostgreSQL introspection differs and is not needed yet)."""
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    missing: dict[str, list[str]] = {}
    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing:
            continue  # created by create_all on this boot; cannot be stale
        if table_name == _META_TABLE:
            continue
        on_disk = {c["name"] for c in inspector.get_columns(table_name)}
        declared = set(table.columns.keys())
        diff = sorted(declared - on_disk)
        if diff:
            missing[table_name] = diff
    return missing


def validate_column_drift(engine) -> dict[str, list[str]]:
    """Return {table: [missing columns]}; raises nothing. Caller decides action."""
    if getattr(engine, "dialect", None) is not None and engine.dialect.name != "sqlite":
        return {}  # PG introspection deferred to the migration phase
    return _missing_declared_columns(engine)


def ensure_schema_compatible(engine, *, expected: int = SCHEMA_VERSION) -> int:
    """Version boundary + column-drift check. Raises SchemaIncompatibleError on mismatch."""
    version = ensure_schema_version(engine, expected=expected)
    drift = validate_column_drift(engine)
    if drift:
        detail = "; ".join(f"{t}: {', '.join(cols)}" for t, cols in sorted(drift.items()))
        raise SchemaIncompatibleError(
            f"on-disk schema is missing declared columns ({detail}); "
            f"run the required migration before startup (schema boundary)."
        )
    return version