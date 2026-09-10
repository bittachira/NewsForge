"""Persistence hardening tests (OPS_HARDENING_PERSISTENCE).

Covers the four hardening domains implemented this phase:
- SQLite durability/concurrency PRAGMAs (WAL, synchronous, busy_timeout, FK off seam),
- deterministic database paths (source-root anchored, DSN passthrough),
- offline snapshot-consistent backup + restore (SQLite Online Backup API),
- schema-version boundary (stamp / newer-refusal / column-drift fail-fast),
plus transaction integrity (rollback, IntegrityError) and restart persistence — the
same behavior the CI persistence step proves inside a real container.

Run: ``python -m pytest tests/test_persistence.py -q``
"""
from __future__ import annotations

import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import delete, func, make_url, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import newsforge.config as config_module
import newsforge.db as db
from newsforge.config import DatabaseConfig
from newsforge.db import (
    SCHEMA_VERSION,
    BackupError,
    SchemaIncompatibleError,
    backup_database,
    restore_database,
    source_items,
    sources,
    stories,
    story_signals,
)
from newsforge.db.schema import ensure_schema_compatible, ensure_schema_version
from newsforge.db.session import build_engine, get_session_factory, init_db


@pytest.fixture(autouse=True)
def isolated_db():
    """Give every test its own throwaway database on the same drive as cwd."""
    db_dir = Path.cwd() / ".pytest_tmp"
    shutil.rmtree(db_dir, ignore_errors=True)
    db_dir.mkdir(exist_ok=True)
    path = db_dir / "persistence.db"
    with db.use_isolated_database_ctx(path):
        yield
    shutil.rmtree(db_dir, ignore_errors=True)


def _count(session, table) -> int:
    return session.execute(select(func.count()).select_from(table)).scalar_one()


# --------------------------------------------------------------------------- #
# SQLite PRAGMAs (durability / concurrency / FK seam)
# --------------------------------------------------------------------------- #
def test_sqlite_pragmas_applied_after_init(tmp_path):
    db_path = tmp_path / "pragmas.db"
    engine = build_engine(DatabaseConfig(path=db_path))
    init_db(engine)
    try:
        with engine.raw_connection() as raw:
            journal = raw.execute("PRAGMA journal_mode").fetchone()[0]
            synchronous = raw.execute("PRAGMA synchronous").fetchone()[0]
            busy = raw.execute("PRAGMA busy_timeout").fetchone()[0]
            foreign_keys = raw.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        engine.dispose()
    assert journal.lower() == "wal"
    assert synchronous == 1  # NORMAL in WAL mode: crash-safe without fsync per commit
    assert busy == 30_000    # wait up to 30s instead of failing with "database is locked"
    assert foreign_keys == 0  # deliberate: business-key FK seam (see below)


def test_fk_seam_business_key_inserts_ok_with_defaults():
    """story_signals.story_id is an FK->stories.id that legitimately holds a business key;
    with the engine defaults (FK OFF) the ORM path works exactly as the publisher uses it."""
    init_db()
    with get_session_factory()() as session:
        story = stories()
        story.story_id = "story-1"
        story.slug = "s1"
        session.add(story)
        src = sources()
        src.source_id = "src-1"
        src.name = "S"
        src.type = "WEBSITE"
        src.language = "es"
        src.tier = "TIER_2"
        src.trust_score = 50
        src.status = "active"
        session.add(src)
        session.flush()
        item = source_items()
        item.source_id = "src-1"
        item.title = "T"
        item.dedupe_hash = "h1"
        session.add(item)
        session.flush()
        signal = story_signals()
        signal.story_id = "story-1"  # BUSINESS KEY, not the stories PK
        signal.item_id = item.id
        session.add(signal)
        session.commit()
    with get_session_factory()() as session:
        assert _count(session, story_signals) == 1


def test_fk_seam_would_break_with_enforcement(tmp_path):
    """Evidence of the seam for the report: on a connection with foreign_keys=ON the
    exact business-key value is rejected, so enforcement is blocked until a migration."""
    db_path = tmp_path / "fk.db"
    engine = build_engine(DatabaseConfig(path=db_path))
    init_db(engine)
    engine.dispose()

    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute(
            "INSERT INTO sources (id, source_id, name, type, language, tier, trust_score, status) "
            "VALUES ('src-1', 'src-1', 'S', 'WEBSITE', 'es', 'TIER_2', 50, 'active')"
        )
        con.execute(
            "INSERT INTO source_items (id, source_id, title, fetched_at, dedupe_hash) "
            "VALUES ('item-1', 'src-1', 'T', '2026-01-01T00:00:00+00:00', 'h1')"
        )
        con.execute(
            "INSERT INTO stories (id, story_id, slug, status, trust_score, created_at, updated_at) "
            "VALUES ('story-pk', 'story-1', 's1', 'ACTIVE', 0, "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            con.execute(
                "INSERT INTO story_signals (id, story_id, item_id, created_at) "
                "VALUES ('sig-1', 'story-1', 'item-1', '2026-01-01T00:00:00+00:00')"
            )
        assert "foreign key" in str(exc_info.value).lower()
        con.rollback()
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Deterministic paths + DSN passthrough
# --------------------------------------------------------------------------- #
def test_build_engine_creates_parent_dir(tmp_path):
    db_path = tmp_path / "a" / "b" / "not_yet_created.db"
    build_engine(DatabaseConfig(path=db_path))
    assert db_path.parent.exists()


def test_default_db_path_anchored_at_source_root(tmp_path, monkeypatch):
    """The default path must NOT depend on the process cwd (§9 persistence hardening)."""
    monkeypatch.delenv("NEWSFORGE_DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = DatabaseConfig()
    resolved = Path(cfg.path)
    assert resolved.is_absolute()
    assert config_module._SOURCE_ROOT in resolved.parents


def test_dsn_passthrough_never_coerced_to_path(monkeypatch):
    """A postgresql:// DSN must survive as a str (Path() would mangle it)."""
    monkeypatch.setenv("NEWSFORGE_DB_PATH", "postgresql://user:pass@db:5432/newsforge")
    cfg = DatabaseConfig()
    assert isinstance(cfg.path, str)
    assert cfg.path.startswith("postgresql://")
    # build_engine's passthrough branch is create_engine(config.path): verify the
    # string is a valid DSN that SQLAlchemy would attach as the postgresql dialect
    # (the psycopg2 driver is not installed locally to build the real engine).
    url = make_url(cfg.path)
    assert url.get_backend_name() == "postgresql"


def test_backup_dir_resolved_and_anchored(tmp_path, monkeypatch):
    monkeypatch.delenv("NEWSFORGE_BACKUP_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = DatabaseConfig()
    assert cfg.backup_dir.is_absolute()
    assert config_module._SOURCE_ROOT in cfg.backup_dir.parents


# --------------------------------------------------------------------------- #
# Backup + restore (SQLite Online Backup API)
# --------------------------------------------------------------------------- #
def _seed_live_db(db_path, slugs):
    engine = build_engine(DatabaseConfig(path=db_path))
    init_db(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        for slug in slugs:
            session.add(stories(story_id=f"sid-{slug}", slug=slug, title=f"T-{slug}"))
        session.commit()
    engine.dispose()


def test_backup_consistent_and_readable(tmp_path):
    db_path = tmp_path / "live.db"
    _seed_live_db(db_path, ["a", "b"])
    backup_path = backup_database(db_path, tmp_path / "backups")
    assert backup_path.is_file()
    assert "newsforge-" in backup_path.name
    con = sqlite3.connect(str(backup_path))
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 2
    finally:
        con.close()


def test_backup_to_explicit_file_path(tmp_path):
    db_path = tmp_path / "live.db"
    _seed_live_db(db_path, ["a"])
    dest = tmp_path / "snap.db"
    backup_database(db_path, dest)
    assert dest.is_file()


def test_backup_missing_source_raises(tmp_path):
    with pytest.raises(BackupError):
        backup_database(tmp_path / "does-not-exist.db", tmp_path / "out")


def test_backup_snapshot_excludes_uncommitted_wal_writes(tmp_path):
    """The backup API snapshots the COMMITTED state: an open writer's uncommitted row
    (present only in the WAL) must not leak into the copy, and must appear after commit."""
    db_path = tmp_path / "wal.db"
    _seed_live_db(db_path, [])
    writer = sqlite3.connect(str(db_path))
    try:
        writer.execute("BEGIN")
        writer.execute(
            "INSERT INTO stories (id, story_id, slug, title, status, trust_score, "
            "created_at, updated_at) VALUES ('w1', 'w1', 'pending', 'P', 'ACTIVE', 0, "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        snap_before = backup_database(db_path, tmp_path / "before-commit.db")
        con = sqlite3.connect(str(snap_before))
        try:
            assert con.execute("SELECT COUNT(*) FROM stories WHERE slug='pending'").fetchone()[0] == 0
        finally:
            con.close()
        writer.commit()
        snap_after = backup_database(db_path, tmp_path / "after-commit.db")
        con = sqlite3.connect(str(snap_after))
        try:
            assert con.execute("SELECT COUNT(*) FROM stories WHERE slug='pending'").fetchone()[0] == 1
        finally:
            con.close()
    finally:
        writer.close()


def test_restore_round_trip(tmp_path):
    db_path = tmp_path / "live.db"
    backup_path = tmp_path / "snap.db"
    _seed_live_db(db_path, ["a", "b"])
    backup_database(db_path, backup_path)

    # Corrupt the LIVE db after the backup: replace its content entirely.
    engine = build_engine(DatabaseConfig(path=db_path))
    try:
        with sessionmaker(bind=engine)() as session:
            session.execute(delete(stories))
            session.add(stories(story_id="x", slug="x", title="X"))
            session.commit()
    finally:
        engine.dispose()

    restore_database(backup_path, db_path)
    engine = build_engine(DatabaseConfig(path=db_path))
    try:
        with sessionmaker(bind=engine)() as session:
            slugs = set(session.scalars(select(stories.slug)).all())
        assert slugs == {"a", "b"}
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# Schema-version boundary
# --------------------------------------------------------------------------- #
def test_schema_version_stamped_on_fresh_db(tmp_path):
    db_path = tmp_path / "v.db"
    engine = build_engine(DatabaseConfig(path=db_path))
    init_db(engine)
    assert ensure_schema_version(engine) == SCHEMA_VERSION
    engine.dispose()


def test_schema_newer_version_refuses_startup(tmp_path):
    db_path = tmp_path / "newer.db"
    engine = build_engine(DatabaseConfig(path=db_path))
    init_db(engine)
    engine.dispose()
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "UPDATE _newsforge_meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION + 1),),
        )
        con.commit()
    finally:
        con.close()
    engine = build_engine(DatabaseConfig(path=db_path))
    try:
        with pytest.raises(SchemaIncompatibleError):
            ensure_schema_compatible(engine)
    finally:
        engine.dispose()


def test_column_drift_fails_fast(tmp_path):
    db_path = tmp_path / "drift.db"
    engine = build_engine(DatabaseConfig(path=db_path))
    init_db(engine)
    engine.dispose()
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("ALTER TABLE stories DROP COLUMN title")
        con.commit()
    finally:
        con.close()
    engine = build_engine(DatabaseConfig(path=db_path))
    try:
        with pytest.raises(SchemaIncompatibleError):
            ensure_schema_compatible(engine)
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# Transaction integrity + restart persistence
# --------------------------------------------------------------------------- #
def test_transaction_rollback_and_commit():
    init_db()
    with get_session_factory()() as session:
        session.add(stories(story_id="keep", slug="keep", title="K"))
        session.commit()
    with get_session_factory()() as session:
        session.add(stories(story_id="drop", slug="drop", title="D"))
        session.rollback()
    with get_session_factory()() as session:
        slugs = set(session.scalars(select(stories.slug)).all())
    assert slugs == {"keep"}


def test_integrity_error_rollback_leaves_clean_state():
    init_db()
    with get_session_factory()() as session:
        session.add(stories(story_id="dup", slug="a"))
        session.commit()
    with get_session_factory()() as session:
        with pytest.raises(IntegrityError):
            session.add(stories(story_id="dup", slug="b"))
            session.commit()
        session.rollback()  # aborted transaction must recover cleanly
        session.add(stories(story_id="ok", slug="c"))
        session.commit()
    with get_session_factory()() as session:
        assert _count(session, stories) == 2


def test_unique_story_id_enforced():
    init_db()
    with get_session_factory()() as session:
        session.add(stories(story_id="dup", slug="a"))
        session.commit()
        with pytest.raises(IntegrityError):
            session.add(stories(story_id="dup", slug="b"))
            session.commit()
        session.rollback()


def test_restart_persistence_reopen_same_file(tmp_path):
    """Rebuild the engine + init on the same file (fresh process equivalent): row survives."""
    db_path = tmp_path / "nf.db"
    engine = build_engine(DatabaseConfig(path=db_path))
    init_db(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        session.add(stories(story_id="r", slug="r", title="R"))
        session.commit()
    engine.dispose()

    engine = build_engine(DatabaseConfig(path=db_path))
    try:
        init_db(engine)
        with sessionmaker(bind=engine)() as session:
            assert _count(session, stories) == 1
    finally:
        engine.dispose()


def test_concurrent_writes_all_committed():
    """Threaded writers serialize on the SQLite write lock (WAL + busy_timeout); every
    commit must land — deterministic, no sleeps, no forced retries."""
    init_db()
    n_threads = 6
    with get_session_factory()() as session:
        session.execute(delete(stories))
        session.commit()

    def worker(i):
        with get_session_factory()() as session:
            session.add(stories(story_id=f"s{i}", slug=f"c{i}", title="W"))
            session.commit()

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(worker, range(n_threads)))

    with get_session_factory()() as session:
        assert _count(session, stories) == n_threads