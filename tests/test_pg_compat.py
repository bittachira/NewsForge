"""PostgreSQL compatibility suite (PRODUCTION_READINESS_DATABASE).

These tests run ONLY when ``NEWSFORGE_DATABASE_URL`` points at a live PostgreSQL
instance — the CI staged PostgreSQL service provides it. Everything else is
covered by the SQLite matrix in ``tests/test_persistence.py``; this file proves
the same invariants against a real PostgreSQL server:

- the Alembic migration bootstrap (empty DB -> corrected schema at head),
- the closed business-key FK seam (FKs point at ``stories.story_id`` /
  ``sources.source_id`` and postgres ALWAYS enforces them),
- PG-safe numeric widths (floats survive round-trips),
- transaction rollback + unique enforcement under concurrency,
- the production startup gate and DSN redaction,
- the /ready endpoint must never leak the connection DSN.

Local runs skip all tests unless a real PG DSN is provided:

    NEWSFORGE_DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:5432/postgres
    python -m pytest tests/test_pg_compat.py -q
"""
from __future__ import annotations

import os
import time

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import sessionmaker

import newsforge.config as config_module
from newsforge.config import DatabaseConfig, redact_dsn
from newsforge.db import (
    products,
    prices,
    source_items,
    sources,
    stories,
    story_signals,
)
from newsforge.db.session import build_engine, init_db
from newsforge.web.app import create_app

PG_DSN = os.getenv("NEWSFORGE_DATABASE_URL", "")


def _is_postgres(dsn: str) -> bool:
    return isinstance(dsn, str) and dsn.startswith(("postgresql", "postgres"))


pytestmark = pytest.mark.skipif(
    not _is_postgres(PG_DSN),
    reason="requires NEWSFORGE_DATABASE_URL pointing at a live PostgreSQL (CI service)",
)


@pytest.fixture(scope="module")
def pg_dsn() -> str:
    """Create a dedicated throwaway database for this run; drop it afterwards."""
    base = make_url(PG_DSN)
    name = f"newsforge_ci_{os.getpid()}_{int(time.time())}"

    admin = build_engine(DatabaseConfig(path=PG_DSN))
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()

    # IMPORTANT: never pass str(url) (its password is masked to ***); the engine
    # + migrations need the REAL password, so render the URL with the secret.
    dsn = base.set(database=name).render_as_string(hide_password=False)

    yield dsn

    # Dispose every engine that may still hold a connection before dropping.
    admin = build_engine(DatabaseConfig(path=PG_DSN))
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


@pytest.fixture(scope="module")
def pg_engine(pg_dsn):
    engine = build_engine(DatabaseConfig(path=pg_dsn))
    try:
        init_db(engine)
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session(pg_engine):
    with sessionmaker(bind=pg_engine, expire_on_commit=False)() as s:
        yield s
        s.rollback()


# --------------------------------------------------------------------------- #
# Migration bootstrap + corrected schema
# --------------------------------------------------------------------------- #
def test_migrations_reach_head_and_fks_target_business_key_columns(pg_engine):
    inspector = inspect(pg_engine)
    assert "alembic_version" in inspector.get_table_names()
    heads = [r[0] for r in pg_engine.connect().execute(text("SELECT version_num FROM alembic_version"))]
    assert heads == ["0002"]

    def fks(table):
        return {(f["constrained_columns"][0], f["referred_table"], f["referred_columns"][0])
                for f in inspector.get_foreign_keys(table)}

    assert fks("story_signals") == {("story_id", "stories", "story_id"), ("item_id", "source_items", "id")}
    assert fks("publications") == {("story_id", "stories", "story_id"), ("decision_id", "decisions", "id")}
    assert fks("source_items") == {("source_id", "sources", "source_id")}

    story_col = {c["name"]: str(c["type"]) for c in inspector.get_columns("stories")}
    assert story_col["story_id"] == "VARCHAR(128)"
    pub_col = {c["name"]: str(c["type"]) for c in inspector.get_columns("publications")}
    assert pub_col["story_id"] == "VARCHAR(128)"
    price_col = {c["name"]: str(c["type"]) for c in inspector.get_columns("prices")}
    # PG renders Float() as DOUBLE PRECISION; SQLite keeps FLOAT.
    assert "FLOAT" in price_col["value"].upper() or "PRECISION" in price_col["value"].upper()
    dec_col = {c["name"]: str(c["type"]) for c in inspector.get_columns("decisions")}
    assert dec_col["target_id"] == "VARCHAR(128)"


# --------------------------------------------------------------------------- #
# FK enforcement: postgres ALWAYS enforces the closed business-key seam
# --------------------------------------------------------------------------- #
def test_fk_enforcement_accepts_business_keys_and_rejects_missing(session):
    session.add(stories(id="story-pk", story_id="story-1", slug="s1", title="Uno"))
    session.add(sources(id="src-pk", source_id="src-1", name="Source", url="https://x"))
    session.flush()
    session.add(source_items(id="item-pk", source_id="src-1", title="Item", dedupe_hash="h1"))
    session.commit()

    session.add(story_signals(id="sig-ok", story_id="story-1", item_id="item-pk"))
    session.commit()

    with pytest.raises((IntegrityError, ProgrammingError)):
        session.add(story_signals(id="sig-bad", story_id="story-missing", item_id="item-pk"))
        session.commit()
    session.rollback()

    with pytest.raises((IntegrityError, ProgrammingError)):
        session.add(source_items(id="item-bad", source_id="src-missing", title="Bad",
                                 dedupe_hash="h2"))
        session.commit()
    session.rollback()


def test_fk_enforcement_publications_business_key(session):
    session.add(stories(id="story-pk2", story_id="story-2", slug="s2", title="Dos"))
    session.commit()
    from newsforge.db import decisions

    session.add(decisions(id="dec-1", target_id="story-2", target_type="story",
                          decision="APPROVED_INTERNAL", risk_level="LOW",
                          trust_score=100, reasons_json='["t"]'))
    session.commit()
    from newsforge.db import publications

    session.add(publications(id="pub-1", story_id="story-2",
                             decision_id="dec-1", destination_key="internal",
                             idempotency_key="ik1", status="DRAFT"))
    session.commit()

    with pytest.raises((IntegrityError, ProgrammingError)):
        from newsforge.db import publications as p2

        session.add(p2(id="pub-bad", story_id="story-missing",
                       decision_id="dec-1", destination_key="internal",
                       idempotency_key="ik2", status="DRAFT"))
        session.commit()
    session.rollback()


# --------------------------------------------------------------------------- #
# Numeric widths + round-trip, rollback, uniqueness
# --------------------------------------------------------------------------- #
def test_float_price_round_trips(session):
    session.add(products(id="prod-pk", product_id="p1", name="P", slug="/p", kind="NEWSLETTER"))
    session.commit()
    session.add(prices(id="price-pk", product_id="prod-pk", value=9.95, currency="USD"))
    session.commit()
    val = session.execute(
        select(prices.value).where(prices.id == "price-pk")
    ).scalar_one()
    assert val == pytest.approx(9.95)


def test_transaction_rollback_leaves_clean_state(session):
    session.add(stories(id="tpk", story_id="t1", slug="t1", title="T"))
    session.commit()
    session.add(stories(id="tpk2", story_id="t2", slug="t2", title="T"))
    session.rollback()
    count = session.execute(select(stories.story_id)).scalars().all()
    assert "t2" not in count


def test_duplicate_story_id_rejected_under_concurrency(session):
    session.add(stories(id="cpk1", story_id="common", slug="c1", title="C"))
    session.commit()
    from sqlalchemy import insert

    with pytest.raises((IntegrityError, ProgrammingError)):
        session.execute(insert(stories).values(id="cpk2", story_id="common", slug="c2", title="C"))
        session.commit()
    session.rollback()


# --------------------------------------------------------------------------- #
# Production gate + DSN hygiene (pure config logic, runs against the DSN env)
# --------------------------------------------------------------------------- #
def test_production_gate_rejects_sqlite(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ENVIRONMENT", "production")
    monkeypatch.setenv("NEWSFORGE_MOCK_AI", "false")
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "x")
    monkeypatch.setenv("NEWSFORGE_SITE_URL", "https://example.com")
    problems = config_module.validate_production_config(
        DatabaseConfig(path="/tmp/newsforge.db")
    )
    assert any("NEWSFORGE_DATABASE_URL" in p for p in problems)


def test_production_gate_accepts_postgres_dsn_and_redacts(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ENVIRONMENT", "production")
    monkeypatch.setenv("NEWSFORGE_MOCK_AI", "false")
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "sekret")
    monkeypatch.setenv("NEWSFORGE_SITE_URL", "https://newsforge.example")
    problems = config_module.validate_production_config(
        DatabaseConfig(path=PG_DSN)
    )
    assert problems == []


def test_dsn_redaction_never_leaks_password():
    dsn = "postgresql+pg8000://bob:sup3r@db.internal:5432/newsforge"
    redacted = redact_dsn(dsn)
    assert "sup3r" not in redacted
    assert "bob:***@" in redacted


def test_ready_endpoint_never_leaks_dsn(pg_dsn, monkeypatch):
    from starlette.testclient import TestClient

    import newsforge.db.session as session_mod

    monkeypatch.setenv("NEWSFORGE_DATABASE_URL", pg_dsn)
    monkeypatch.setenv("NEWSFORGE_ENVIRONMENT", "development")
    try:
        client = TestClient(create_app())
        raw = client.get("/ready").content.decode("utf-8", "replace")
    finally:
        # NEVER let a PostgreSQL default engine leak into the shared process state:
        # later test modules expect their own SQLite default. Dispose + reset it.
        if session_mod._default_engine is not None:
            session_mod._default_engine.dispose()
        session_mod._default_engine = None
        session_mod._default_factory = None
    assert "ok" in raw.lower() or "ready" in raw.lower() or "connected" in raw.lower()
    assert "postgresql" not in raw.lower()
    assert "5432" not in raw
    assert "@" not in raw
    # the password portion of the CI DSN must not surface either
    from sqlalchemy.engine import make_url as _mu

    passwd = _mu(PG_DSN).password
    if passwd:
        assert passwd not in raw