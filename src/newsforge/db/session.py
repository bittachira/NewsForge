"""Session factory + engine builder. SQLite-first, PostgreSQL-ready."""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from newsforge.config import DatabaseConfig
from newsforge.core.logger import get_logger
from newsforge.db.base import Base
from newsforge.db.schema import ensure_schema_compatible

logger = get_logger("db.session")

# SQLite durability/concurrency tunables (OPS_HARDENING_PERSISTENCE).
_SQLITE_BUSY_TIMEOUT_MS = 30_000


def _apply_sqlite_pragmas(dbapi_connection, connection_record):  # noqa: ARG001 - listener signature
    """Per-connection SQLite configuration applied whenever a new connection opens.

    - ``journal_mode=WAL``: concurrent readers + one writer, far less disk sync than the
      default rollback journal (safe for the multi-worker uvicorn model).
    - ``synchronous=NORMAL``: in WAL mode this keeps the DB consistent/crash-safe without
      fsyncing on every commit (the durability/speed trade-off recommended by SQLite).
    - ``busy_timeout=30000``: wait up to 30s for a lock instead of failing immediately
      with "database is locked".
    - ``foreign_keys=OFF`` **deliberately for SQLite only**: enforcement in SQLite is a
      per-connection PRAGMA with different semantics than PostgreSQL, and WAL + deferred
      FK checks add lock surface that the MVP concurrency model does not need. The
      business-key FK seam was closed in the schema itself (foreign keys now point at the
      unique ``stories.story_id`` / ``sources.source_id`` columns), so PostgreSQL — which
      ALWAYS enforces foreign keys — validates the same invariant without any code change.
    """
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    cur.execute("PRAGMA foreign_keys=OFF")
    cur.close()


def build_engine(config: DatabaseConfig | None = None) -> create_engine:
    """Build a SQLAlchemy engine.

    - SQLite file (default MVP): ``sqlite:///data/newsforge.db``
    - PostgreSQL/other DSNs are supported by setting NEWSFORGE_DB_PATH to a full DSN,
      which swaps the dialect without touching any model code (§34 migration path).
    """
    config = config or DatabaseConfig()

    # Non-SQLite DSNs (PostgreSQL, MySQL, ...) are passed through verbatim.
    if isinstance(config.path, str) and config.path.startswith(
        ("postgresql", "postgres", "mysql", "mariadb")
    ):
        logger.info("Opening database at %s", config.redacted_path)  # password NEVER logged
        return create_engine(config.path, echo=config.echo_sql, future=True)

    path = Path(config.path)
    abs_path = Path(os.path.abspath(path))  # canonical absolute OS path (drive-safe on Windows)
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Opening database at %s", abs_path)
    kwargs = {"future": True, "connect_args": {"check_same_thread": False,
                                               "timeout": _SQLITE_BUSY_TIMEOUT_MS // 1000}}

    # On Windows the sqlite:// URL scheme cannot open absolute paths that carry a
    # drive letter (create_all and later queries would target different files).
    # Resolve same-drive absolute paths to cwd-relative ones, which SQLAlchemy
    # opens reliably. Relative inputs are used as-is.
    if sys.platform == "win32" and abs_path.is_absolute() \
            and abs_path.drive == Path.cwd().drive:
        rel = Path(os.path.relpath(abs_path, Path.cwd())).as_posix()  # forward slashes for the URL
        engine = create_engine(f"sqlite:///{rel}", **kwargs)
    else:
        # POSIX (absolute path) or cross-drive Windows fallback.
        posix = str(abs_path).replace(os.sep, "/")
        if posix.startswith("/"):
            posix = posix[1:]  # SQLAlchemy canonical absolute URL is sqlite:////<path> (no leading slash)
        # A Windows drive-letter absolute path (C:/...) must use the 3-slash form:
        # sqlite3 rejects sqlite:////C:/... on Windows (it resolves the leading "/"
        # against the cwd drive). POSIX absolute paths use the 4-slash form.
        drive_letter = len(posix) >= 2 and posix[1] == ":"
        url = f"sqlite:////{posix}" if abs_path.is_absolute() and not drive_letter else f"sqlite:///{posix}"
        engine = create_engine(url, **kwargs)

    event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


# Shared default engine + factory. Tests override these with isolated databases.
_default_engine: create_engine | None = None
_default_factory: sessionmaker | None = None


def _ensure_shared() -> tuple[create_engine, sessionmaker]:
    """Return the shared (engine, factory) pair, building them on first use."""
    global _default_engine, _default_factory
    if _default_engine is None:
        _default_engine = build_engine()
    if _default_factory is None or getattr(_default_factory, "bind", None) is not _default_engine:
        _default_factory = sessionmaker(
            bind=_default_engine, autocommit=False, autoflush=True, expire_on_commit=False
        )
    return _default_engine, _default_factory


def set_session_factory(factory: sessionmaker) -> None:
    """Override the shared factory (used by tests with isolated databases)."""
    global _default_factory, _default_engine
    _default_factory = factory
    _default_engine = None  # force a fresh engine bound to the new factory


def use_isolated_database(path) -> None:
    """Point the shared (engine, factory) at an isolated DB. Used by tests."""
    global _default_engine, _default_factory
    # Release connections/locks of the previous engine so its file can be removed.
    if _default_engine is not None:
        _default_engine.dispose()
    _default_engine = build_engine(DatabaseConfig(path=path))
    Base.metadata.create_all(bind=_default_engine)
    _default_factory = sessionmaker(
        bind=_default_engine, autocommit=False, autoflush=True, expire_on_commit=False
    )


def switch_default_database(path) -> None:
    """Point the shared DB at ``path`` (tests). Does NOT restore — caller manages that."""
    global _default_engine, _default_factory
    use_isolated_database(path)


@contextmanager
def use_isolated_database_ctx(path):
    """Context manager: isolate the shared DB at ``path`` and restore on exit (tests)."""
    global _default_engine, _default_factory
    prev_engine, prev_factory = _default_engine, _default_factory
    try:
        use_isolated_database(path)
        yield
    finally:
        # Release the current engine's connections/locks before restoring so its
        # file can be removed on Windows.
        if _default_engine is not None:
            _default_engine.dispose()
        _default_engine, _default_factory = prev_engine, prev_factory


def get_session_factory() -> sessionmaker:
    """Return the shared session factory (building it on first use)."""
    _, factory = _ensure_shared()
    return factory


def is_database_initialized() -> bool:
    """True when the shared engine has been built already.

    Used by best-effort subsystems (e.g. error tracking) so they can probe for an
    available database WITHOUT triggering engine creation as a side effect."""
    return _default_engine is not None


@contextmanager
def get_session() -> Iterator[Session]:
    """Context manager yielding a Session bound to the shared engine."""
    _, factory = _ensure_shared()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _alembic_upgrade(engine) -> None:
    """Run Alembic migrations to head on ``engine`` (SQLite + PostgreSQL).

    Migration state is tracked by the ``alembic_version`` table owned by
    :mod:`newsforge.db.migrations`. Three starts are possible:

    * **Fresh database** (no tables): run every migration (0001 creates the
      corrected baseline, 0002 seals the business-key seam — both are no-ops in
      terms of data because the schema was authored corrected already).
    * **Legacy ``create_all`` database** (tables, no ``alembic_version``):
      adopt it as the 0001 baseline (after the schema boundary accepts it) and
      upgrade so the business-key rebase (0002) applies in place.
    * **Already migrated**: a simple ``upgrade head`` (a cheap no-op).
    """
    from sqlalchemy import inspect as sa_inspect

    from alembic import command
    from alembic.config import Config

    # The migrations directory ships INSIDE the package so a bare copy of `src`
    # (the Docker image) can bootstrap the schema without extra assets.
    migrations_dir = Path(__file__).parent / "migrations"
    cfg = Config(str(migrations_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option(
        "sqlalchemy.url", engine.url.render_as_string(hide_password=False)
    )
    # Migrations are quiet by default: they inherit the app's structured logging
    # (logger level WARN) so a normal startup produces no migration chatter.
    import logging

    logging.getLogger("alembic").setLevel(logging.WARNING)
    logging.getLogger("alembic.runtime.migration").setLevel(logging.WARNING)

    tables = set(sa_inspect(engine).get_table_names())
    if "alembic_version" not in tables and any(t for t in tables if t != "alembic_version"):
        # Legacy create_all-era database: the pre-migration schema was the
        # equivalence of revision 0001; stamp it then upgrade to head.
        ensure_schema_compatible(engine)
        command.stamp(cfg, "0001")
    command.upgrade(cfg, "head")


def init_db(engine: create_engine | None = None) -> None:
    """Create/migrate the schema + validate the schema boundary. Idempotent on startup.

    Defaults to the shared engine (the one ``get_session``/routes use) so the
    schema is always migrated on the database that serves requests — even when
    tests swapped in an isolated database.

    Schema management is handled by Alembic (``newsforge.db.migrations``),
    which runs ``upgrade head`` programmatically. Tests and ``use_isolated_database``
    keep ``Base.metadata.create_all`` as the fast path (schema parity is asserted
    by ``tests`` that compare both). After migrations the
    :func:`~newsforge.db.schema.ensure_schema_compatible` boundary runs: it stamps
    ``_newsforge_meta.schema_version`` on a fresh DB and REFUSES to start when the
    on-disk version differs from this build or a declared column is missing
    (fail-fast instead of runtime ``no such column`` errors).
    """
    engine = engine or _ensure_shared()[0]
    if engine.dialect.name == "sqlite" and not getattr(engine.url, "database", None):
        # In-memory SQLite: Alembic's engine_from_config opens its own connection
        # and would rebuild an empty volatile database; keep the in-place path.
        Base.metadata.create_all(bind=engine)
        version = ensure_schema_compatible(engine)
        logger.info("Database schema ready (schema_version=%s).", version)
        return
    _alembic_upgrade(engine)
    version = ensure_schema_compatible(engine)
    logger.info("Database schema ready (schema_version=%s).", version)
