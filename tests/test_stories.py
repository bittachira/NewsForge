"""Story Engine gate tests (§4) — detection + persistence.

These are the P2 verification gate: they prove that (a) unrelated signals never merge
into one story, (b) related signals cluster under a stable STORY_ID with an entity prefix,
(c) :meth:`StoryDetector.process` is idempotent and links items correctly, and (d) story
trust aggregates from contributing sources.

Run: ``python -m pytest tests/test_stories.py -q``
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

import newsforge.db as db
from newsforge.db import get_session, source_items, stories, story_signals, sources
from newsforge.stories.detector import classify_topic, cluster_items, detect_story_id
from newsforge.stories.engine import StoryDetector


@pytest.fixture(autouse=True)
def isolated_db():
    """Give every test its own throwaway database on the same drive as cwd."""
    import shutil as _shutil

    db_dir = Path.cwd() / ".pytest_tmp"
    _shutil.rmtree(db_dir, ignore_errors=True)
    db_dir.mkdir(exist_ok=True)
    path = db_dir / "test.db"
    with db.use_isolated_database_ctx(path):
        yield
    # Cleanup: the previous engine was disposed on switch, so its file lock is free.
    _shutil.rmtree(db_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Helpers — seed a source + item directly (deterministic tiers, no network)
# --------------------------------------------------------------------------- #
def _seed_item(session, *, title, description, published_at="2026-09-05T10:00:00+00:00", tier="TIER_2"):
    src = sources()
    src.source_id = f"src-{title}"
    src.name = "Test Source"
    src.type = "RSS"
    src.country = "ES"
    src.language = "es"
    src.tier = tier
    src.trust_score = 60
    src.status = "active"
    session.add(src)

    item = source_items()
    item.source_id = src.source_id
    item.title = title
    item.description = description
    item.content_html = None
    item.content_text = description
    item.published_at = published_at
    item.dedupe_hash = f"hash-{title}"
    session.add(item)
    session.commit()  # persist so a later StoryDetector.process sees the rows
    return str(item.id), src.tier


def _count_stories():
    with get_session() as s:
        return s.query(stories).count()


def _count_links_for_story(session, story_id):
    return len(session.query(story_signals).filter_by(story_id=story_id).all())


# --------------------------------------------------------------------------- #
# Regression tests — the two bugs found in the untested detector
# --------------------------------------------------------------------------- #
def test_classify_topic_returns_empty_when_nothing_matches():
    """Unrelated text must NOT be mislabeled as a topic (Bug 1 regression)."""
    slug, score = classify_topic("Otra historia totalmente distinta sin tema")
    assert slug == "" and score == 0.0


def test_classify_topic_detects_real_topics():
    slug, _ = classify_topic("La UE aprueba un impuesto sobre paquetes pequeños en 2026")
    assert slug == "package_tax" and _ > 0


def test_unrelated_items_do_not_merge_into_one_story():
    """Bug 2 regression: unrelated signals must land in separate stories."""
    items = [
        {"title": "Nuevo impuesto a paquetes pequeños", "description": "La UE aprueba un impuesto sobre paquetes pequenos en 2026"},
        {"title": "Impuesto de paquetes pequenos europeo", "description": "El pequeno paquete sufre el nuevo impuesto de la Union Europea"},
        {"title": "Otra historia totalmente distinta", "description": "algo completamente diferente sin tema claro"},
    ]
    clusters = cluster_items(items)
    assert len(clusters) == 3, f"unrelated items merged: {list(clusters)}"


def test_related_items_cluster_with_entity_prefix():
    """Related signals sharing a topic+year group together; shared entity prefixes the id."""
    items = [
        {"title": "European Union Package Tax", "description": "The European Union introduces a small package tax in 2026"},
        {"title": "Small package tax in the European Union", "description": "European Union members implement the small package tax for 2026"},
    ]
    clusters = cluster_items(items)
    assert len(clusters) == 1
    story_id = next(iter(clusters))
    # Shared entity prefix (clean ASCII, appears in both items) + topic + year.
    assert story_id.startswith("european_union_package_tax_2026")


def test_detect_is_order_independent():
    a = cluster_items([{"title": "A", "description": "impuesto paquete"}, {"title": "B", "description": "otro tema"}])
    b = cluster_items([{"title": "B", "description": "otro tema"}, {"title": "A", "description": "impuesto paquete"}])
    assert set(a.keys()) == set(b.keys())


# --------------------------------------------------------------------------- #
# Engine: persistence, idempotency, trust, links
# --------------------------------------------------------------------------- #
def test_process_creates_one_story_per_item_and_links_it():
    with get_session() as s:
        _seed_item(s, title="Impuesto paquetes", description="La UE aprueba un impuesto sobre paquetes en 2026")
        item_id = s.query(source_items).filter_by(title="Impuesto paquetes").first().id

    result = StoryDetector().process(signal_ids=[item_id])
    assert result.created_stories == 1
    with get_session() as s:
        story = s.query(stories).filter_by(story_id=result.stories[0]["story_id"]).one()
        assert len(s.query(story_signals).filter_by(story_id=story.story_id).all()) == 1


def test_process_is_idempotent_across_repeated_runs():
    with get_session() as s:
        _seed_item(s, title="Impuesto paquetes", description="La UE aprueba un impuesto sobre paquetes en 2026")
        item_id = s.query(source_items).filter_by(title="Impuesto paquetes").first().id

    first = StoryDetector().process(signal_ids=[item_id])
    with get_session() as s:
        links_after_first = len(s.query(story_signals).all())

    second = StoryDetector().process(signal_ids=[item_id])
    assert second.created_stories == 0, "re-running must not create a duplicate story"
    assert second.updated_stories == 1
    with get_session() as s:
        links_after_second = len(s.query(story_signals).all())
    # No duplicate links (unique constraint + idempotent insert logic).
    assert links_after_first == links_after_second


def test_story_trust_aggregates_from_contributing_sources():
    """A story fed by TIER_1 sources scores higher than one fed by TIER_4."""
    with get_session() as s:
        _seed_item(s, title="Tier1 tax", description="impuesto paquete pequeño UE 2026", tier="TIER_1")
        t1_id = s.query(source_items).filter_by(title="Tier1 tax").first().id
        _seed_item(s, title="Tier4 rumor", description="rumor de impuesto paquete", tier="TIER_4")
        t4_id = s.query(source_items).filter_by(title="Tier4 rumor").first().id

    high = StoryDetector().process(signal_ids=[t1_id]).stories[0]["trust_score"]
    low = StoryDetector().process(signal_ids=[t4_id]).stories[0]["trust_score"]
    assert high > low, f"TIER_1 story trust {high} should exceed TIER_4 {low}"


def test_related_signals_share_one_story_and_link_count_is_stable():
    with get_session() as s:
        _seed_item(s, title="Tax A", description="European Union small package tax 2026")
        a_id = s.query(source_items).filter_by(title="Tax A").first().id
        _seed_item(s, title="Tax B", description="Small package tax European Union 2026")
        b_id = s.query(source_items).filter_by(title="Tax B").first().id

    result = StoryDetector().process(signal_ids=[a_id, b_id])
    assert result.created_stories == 1
    with get_session() as s:
        story = s.query(stories).one()
        # Both items linked to the single shared story; no orphans.
        total_links = len(s.query(story_signals).all())
        assert total_links == 2
        for sid in (a_id, b_id):
            assert _count_links_for_story(s, story.story_id) == 2


# --------------------------------------------------------------------------- #
# Production FK regression — story_signals must never precede its stories row.
# SQLite leaves foreign keys OFF by design (db/session.py), so these tests opt in
# to `PRAGMA foreign_keys=ON` and reproduce the exact order PostgreSQL requires.
# --------------------------------------------------------------------------- #
def _sqlite_fk_on(dbapi_connection, connection_record):  # noqa: ARG001
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


def _enable_fk_pragma():
    """Force FK enforcement for the shared engine used by this test.

    SQLite treats FK enforcement as a per-connection setting and the app's connect
    listener turns it OFF, so (a) register a listener that turns it ON for every
    future connection, then (b) dispose pooled connections created earlier (e.g. by
    ``create_all``) so they are replaced by fresh ones that carry the pragma.
    """
    import newsforge.db.session as _session_mod
    from sqlalchemy import event

    engine = _session_mod._default_engine
    event.listen(engine, "connect", _sqlite_fk_on, once=False)
    engine.dispose()


def test_sqlite_fk_enforcement_is_active_in_regressions():
    """Guard: prove the regression fixtures really enforce the story FK."""
    _enable_fk_pragma()
    with get_session() as s:
        s.add(sources(source_id="src-fk", name="S", type="RSS", country="ES",
                      language="es", trust_score=60, status="active"))
        s.commit()
    with get_session() as s:
        item = source_items(source_id="src-fk", title="I", description="d",
                            dedupe_hash="h-fk")
        s.add(item)
        s.commit()
        with pytest.raises(IntegrityError):
            s.add(story_signals(story_id="st-missing", item_id=str(item.id)))
            s.commit()
        s.rollback()


def _seed_item_fk(session, *, title, description,
                  published_at="2026-09-05T10:00:00+00:00"):
    """Seed source + item like the ingest phase really does (source first)."""
    src = sources()
    src.source_id = f"src-{title}"
    src.name = "Test Source"
    src.type = "RSS"
    src.country = "ES"
    src.language = "es"
    src.tier = "TIER_2"
    src.trust_score = 60
    src.status = "active"
    session.add(src)
    session.commit()

    item = source_items()
    item.source_id = src.source_id
    item.title = title
    item.description = description
    item.content_html = None
    item.content_text = description
    item.published_at = published_at
    item.dedupe_hash = f"hash-{title}"
    session.add(item)
    session.commit()
    return str(item.id)


def test_regression_story_parent_ordered_before_signals_with_fk():
    """Production DETECT bug: story_signals inserted before its stories row.

    Reproduces the Render crash (``story_signals_story_id_fkey`` with
    ``story_id='september_2026'``) on SQLite with FK enforcement ON. Two items
    sharing the capitalized entity "September" + year 2026 resolve to story_id
    ``september_2026``; the engine must persist the stories row before linking.
    """
    _enable_fk_pragma()
    with get_session() as s:
        _seed_item_fk(s, title="September 2026 report",
                      description="september 2026 regional outlook report")
        _seed_item_fk(s, title="September 2026 analysis",
                      description="september 2026 briefing for the board")
    with get_session() as s:
        ids = [str(r.id) for r in s.query(source_items).all()]

    first = StoryDetector().process(signal_ids=ids)
    assert first.created_stories == 1
    assert first.linked_signals == 2
    assert first.stories[0]["story_id"] == "september_2026"

    # The FK target now exists and both signals are linked, in one transaction.
    with get_session() as s:
        story = s.query(stories).filter_by(story_id="september_2026").one()
        assert len(s.query(story_signals).filter_by(story_id=story.story_id).all()) == 2

    # Idempotent re-run: no duplicate stories/links, never touches the FK.
    second = StoryDetector().process(signal_ids=ids)
    assert second.created_stories == 0
    assert second.updated_stories == 1
    assert second.linked_signals == 0
    with get_session() as s:
        assert s.query(stories).filter_by(story_id="september_2026").count() == 1
        assert s.query(story_signals).filter_by(story_id="september_2026").count() == 2


def test_process_requires_input():
    with pytest.raises(ValueError):
        StoryDetector().process()
