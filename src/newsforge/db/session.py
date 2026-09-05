"""Session factory + engine builder. SQLite-first, PostgreSQL-ready."""
from __future__ import annotations

import os
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

    if isinstance(config.path, str) and (config.path.startswith("postgresql") or config.path.startswith("postgres")):
        url = config.path
        engine = create_engine(url, echo=config.echo_sql, future=True)
        return engine

    path = Path(config.path)
    if path.parent and not path.exists() and path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)

    url = f"sqlite:///{path}"
    logger.info("Opening database at %s", path)
    engine = create_engine(url, echo=config.echo_sql, future=True, connect_args={"check_same_thread": False})
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
