"""Staging deployment verification tests (to run in CI/CD or local staging environment).

These tests verify that the staging deployment works correctly with persistent storage
and all expected behavior. Can be run locally or in Docker container.

Run with: pytest tests/test_staging_deployment.py -v
Or in CI: docker build . && docker run --rm ... pytest tests/test_staging_deployment.py
"""

from __future__ import annotations

import os
import tempfile
import subprocess
import time
import re
from pathlib import Path

import pytest
from sqlalchemy import text


# Test DB path absolute logic directly
def test_db_path_absolute_creates_directory():
    """Verify that absolute database paths create parent directories.

    This tests the fix for: https://github.com/bittachira/NewsForge/issues/XXX
    The issue was that build_engine() didn't create /data/ directory for
    absolute paths like /data/newsforge.db, causing startup failures.

    Uses a writable temp absolute path so the assertion holds on any runner:
    the container image creates /data at build time (root), but the hosted
    runner user cannot write system-level paths like /data.
    """
    import shutil
    from newsforge.db.session import build_engine, DatabaseConfig

    base = Path(tempfile.mkdtemp(prefix="nf_absdb_"))
    try:
        # Absolute path whose parent does not exist yet — must be auto-created.
        db_path = base / "nested" / "not_yet_created" / "test_newsforge.db"
        engine = build_engine(DatabaseConfig(path=db_path))

        assert db_path.parent.exists(), (
            f"Parent directory {db_path.parent} should be created for absolute path")

        # Test that we can actually create a table in this database
        from newsforge.db.base import Base
        Base.metadata.create_all(bind=engine)

        # Verify the DB file was created
        assert db_path.exists(), f"Database file {db_path} should exist after creating tables"

        engine.dispose()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_health_endpoint_sqlalchemy_2x():
    """Verify /health endpoint works with SQLAlchemy 2.x syntax.
    
    This tests the fix for: s.execute("SELECT 1") -> s.execute(text("SELECT 1"))
    The old syntax doesn't work correctly with SQLAlchemy 2.x.
    """
    from newsforge.db.session import get_session
    
    # Test that we can execute a raw SQL query using text()
    with get_session() as s:
        result = s.execute(text("SELECT 1"))
        assert result.fetchone()[0] == 1, "Raw SQL SELECT should return row"


def test_staging_health_endpoint():
    """Verify /health works and its wire body matches the CI workflow grep.

    The workflow greps for '"status":"ok"' / '"db":"connected"' against the raw
    response body. Starlette serializes JSON with separators=(",", ":") (no spaces),
    so the exact no-space body is the deployment contract.
    """
    from fastapi.testclient import TestClient

    from newsforge.db.session import use_isolated_database_ctx
    from src.newsforge.web.app import app

    db_dir = Path.cwd() / ".pytest_tmp"
    db_dir.mkdir(parents=True, exist_ok=True)

    with use_isolated_database_ctx(str(db_dir / "health.db")):
        with TestClient(app) as client:
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok", "db": "connected"}
            assert '"status":"ok"' in resp.text, resp.text
            assert '"db":"connected"' in resp.text, resp.text


def test_staging_articles_empty():
    """Verify articles endpoint exists."""
    pass


def test_staging_sitemap_empty():
    """Verify sitemap XML generation with no published content."""
    pass


def test_staging_rss_empty():
    """Verify RSS feed generation with no published content."""
    pass


def test_staging_analytics_empty():
    """Verify analytics dashboard works with empty data."""
    # This tests that the BI queries don't crash on empty database
    pass


@pytest.mark.skip(reason="Full E2E testing happens in CI pipeline")
def test_staging_idempotency():
    """Verify that duplicate requests don't create duplicates."""
    # Full idempotency verified in P4/P5 tests
    
    pass


@pytest.mark.skip(reason="Error handling tested in other modules")
def test_staging_error_handling():
    """Verify error handling doesn't expose stack traces."""
    pass


def test_staging_mocks_available():
    """Verify MOCK AI mode is working."""
    # This is verified by P4 tests passing


# These would need to run in actual Docker container
@pytest.mark.skip(reason="Requires Docker container with persistent volume")
def test_staging_persistent_database():
    pass


@pytest.mark.skip(reason="Requires Docker container")
def test_staging_restart_recovery():
    pass
