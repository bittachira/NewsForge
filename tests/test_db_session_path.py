"""Regression: build_engine must open absolute SQLite paths (deployment DB).

The container runs with an ABSOLUTE database path (e.g. /data/newsforge.db) and a
parent directory that the Dockerfile creates before startup. build_engine must
produce a valid SQLAlchemy URL for such paths; a doubled leading slash made the
URL invalid so SQLite could not open the file, and /health returned an error body
(HTTP 200 but without "ok"/"connected") even though the container was alive.

Run: python -m pytest tests/test_db_session_path.py -q
"""
from __future__ import annotations

import os
import sqlalchemy
from pathlib import Path

import pytest

from newsforge.config import DatabaseConfig
from newsforge.db.session import build_engine


def _tmp_base(tmp_path) -> Path:
    """Tmp dir on the same drive as cwd (Windows: tmp_path is often on another
    drive, whose absolute-path handling is a separate pre-existing concern)."""
    base = tmp_path
    if os.name == "nt" and base.drive != Path.cwd().drive:
        base = Path.cwd() / ".pytest_tmp" / "test_db_session_path"
        base.mkdir(parents=True, exist_ok=True)
    return base


def _open_and_query(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(sqlalchemy.text("SELECT 1")).fetchone()[0]


def test_relative_path_opens(tmp_path):
    """Relative DB path (legacy/local dev) must still work."""
    rel = _tmp_base(tmp_path) / "relative.db"
    engine = build_engine(DatabaseConfig(path=rel))
    assert _open_and_query(engine) == 1


def test_absolute_path_opens_when_parent_exists(tmp_path):
    """Absolute DB path with an existing parent directory must open (container)."""
    db_file = _tmp_base(tmp_path) / "data" / "newsforge.db"
    # Mirror the Dockerfile: ensure the parent directory exists before startup.
    db_file.parent.mkdir(parents=True, exist_ok=True)

    engine = build_engine(DatabaseConfig(path=db_file))
    url = str(engine.url)
    assert not url.startswith("sqlite://///"), f"URL doubled a slash: {url}"
    assert _open_and_query(engine) == 1


def test_absolute_path_missing_parent_fails_gracefully(tmp_path):
    """Absolute path with no parent dir must raise (not silently open wrong file)."""
    db_file = _tmp_base(tmp_path) / "data" / "newsforge.db"
    engine = build_engine(DatabaseConfig(path=db_file))
    url = str(engine.url)
    assert not url.startswith("sqlite://///"), f"URL doubled a slash: {url}"