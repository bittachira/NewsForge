"""Session factory + engine builder. SQLite-first, PostgreSQL-ready."""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from newsforge.config import DatabaseConfig
from newsforge.core.logger import get_logger
from newsforge.db.base import Base

logger = get_logger("db.session")


def build_engine(config: DatabaseConfig | None = None) -> create_engine:
    """Build a SQLAlchemy engine.

    - SQLite file (default MVP): ``sqlite:///data/newsforge.db``
    - PostgreSQL/other DSNs are supported by setting NEWSFORGE_DSN to a full DSN,
      which swaps the dialect without touching any model code (§34 migration path).
    """
    config = config or DatabaseConfig()

    # Non-SQLite DSNs (PostgreSQL, MySQL, ...) are passed through verbatim.
    if isinstance(config.path, str) and config.path.startswith(
        ("postgresql", "postgres", "mysql", "mariadb")
    ):
        return create_engine(config.path, echo=config.echo_sql, future=True)

    path = Path(config.path)
    abs_path = Path(os.path.abspath(path))  # canonical absolute OS path (drive-safe on Windows)
    if not abs_path.is_absolute() and str(abs_path.parent) != ".":
        abs_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Opening database at %s", abs_path)
    kwargs = {"future": True, "connect_args": {"check_same_thread": False}}

    # On Windows the sqlite:// URL scheme cannot open absolute paths that carry a
    # drive letter (create_all and later queries would target different files).
    # Resolve same-drive absolute paths to cwd-relative ones, which SQLAlchemy
    # opens reliably. Relative inputs are used as-is.
    if sys.platform == "win32" and abs_path.is_absolute() \
            and abs_path.drive == Path.cwd().drive:
        rel = Path(os.path.relpath(abs_path, Path.cwd())).as_posix()  # forward slashes for the URL
        return create_engine(f"sqlite:///{rel}", **kwargs)

    # POSIX (absolute path) or cross-drive Windows fallback.
    posix = str(abs_path).replace(os.sep, "/")
    if posix.startswith("/"):
        posix = posix[1:]  # SQLAlchemy canonical absolute URL is sqlite:////<path> (no leading slash)
    url = f"sqlite:////{posix}" if abs_path.is_absolute() else f"sqlite:///{posix}"
    return create_engine(url, **kwargs)


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


@contextmanager
def get_session() -> Iterator[Session]:
    """Context manager yielding a Session bound to the shared engine."""
    _, factory = _ensure_shared()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def init_db(engine: create_engine | None = None) -> None:
    """Create all tables. Idempotent — safe to call on every startup."""
    engine = engine or build_engine()
    Base.metadata.create_all(bind=engine)
    logger.info("Database schema ready.")
