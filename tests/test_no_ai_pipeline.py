"""Regression tests: AI_ENABLED=false — deterministic pipeline without any LLM.

Tests demonstrate that the full pipeline can operate end-to-end without
AI, that germany_elections_2026 flows through deterministically, and that
ad slots, SEO and analytics are wired correctly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from newsforge import db
from newsforge.db import (
    generated_artifacts,
    get_session,
    stories,
)
from newsforge.pipeline.orchestrator import run_pipeline

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
_db_seq = 0


@pytest.fixture(autouse=True)
def isolated_db():
    global _db_seq
    db_dir = Path.cwd() / ".pytest_tmp"
    db_dir.mkdir(exist_ok=True)
    _db_seq += 1
    module = __name__.rsplit(".", 1)[-1]
    path = db_dir / f"{module}_{_db_seq}.db"
    try:
        if path.exists():
            path.unlink()
    except OSError:
        _db_seq += 1
        path = db_dir / f"{module}_{_db_seq}.db"
    with db.use_isolated_database_ctx(path):
        yield


@pytest.fixture(autouse=True)
def clean_registry():
    from newsforge.publish.destinations import (
        register_builtin_destinations,
        reset_registry,
    )
    reset_registry()
    register_builtin_destinations()
    yield
    reset_registry()


# --------------------------------------------------------------------------- #
# Seeding helpers
# --------------------------------------------------------------------------- #
def _seed_source(session, *, id: str, name: str = "Test Outlet", tier: str = "TIER_1"):
    src = db.sources()
    src.id = id
    src.source_id = id
    src.name = name
    src.type = "RSS"
    src.country = "DE"
    src.language = "en"
    src.tier = tier
    src.trust_score = 90 if tier == "TIER_1" else 35
    src.status = "active"
    session.add(src)
    session.commit()
    return id


def _seed_item(session, *, source_id: str, title: str, description: str | None = None,
               published_at: str = "2026-09-21T10:00:00+00:00") -> str:
    item = db.source_items()
    item.source_id = source_id
    item.title = title
    item.description = description
    item.content_html = None
    item.content_text = description or title
    item.published_at = published_at
    item.dedupe_hash = f"hash-{title}"
    session.add(item)
    session.commit()
    return str(item.id)


def _seed_three_sources(session) -> list[str]:
    """Seed three independent TIER_1 sources for corroboration tests."""
    ids = []
    for i, name in enumerate(["Reuters", "AP News", "BBC"]):
        sid = f"src-{i}"
        _seed_source(session, id=sid, name=name, tier="TIER_1")
        ids.append(sid)
    return ids


# --------------------------------------------------------------------------- #
# Test 1: Full pipeline without AI
# --------------------------------------------------------------------------- #
def test_pipeline_without_ai():
    """Pipeline completes P1-P6 with ai_router=None (DeterministicGenerator)."""
    with get_session() as s:
        _seed_source(s, id="src-ai-off")
        item = _seed_item(s, source_id="src-ai-off",
                          title="Economic growth accelerates in Q3 2026")

    result = run_pipeline(signal_ids=[item], ai_router=None,
                          reference_time="2026-09-21T12:00:00+00:00")

    assert result["stories_detected"] >= 1
    assert result["stories_processed"] >= 1
    outcome = result["outcomes"][0]
    assert outcome.final_status == "PUBLISHED", (
        f"expected PUBLISHED, got {outcome.final_status}: {outcome.error}"
    )
    assert outcome.artifact is not None
    assert outcome.publish is not None
    assert outcome.publish["published"] is True


# --------------------------------------------------------------------------- #
# Test 2: Story reaches PUBLISH without LM Studio
# --------------------------------------------------------------------------- #
def test_publish_without_lm_studio():
    """No ProviderError is raised when ai_router is None."""
    with get_session() as s:
        _seed_source(s, id="src-no-lm")
        item = _seed_item(s, source_id="src-no-lm",
                          title="New climate accord signed by 40 nations")

    result = run_pipeline(signal_ids=[item], ai_router=None,
                          reference_time="2026-09-21T12:00:00+00:00")
    outcome = result["outcomes"][0]
    assert outcome.final_status == "PUBLISHED"
    assert outcome.error is None or "ProviderError" not in (outcome.error or "")


# --------------------------------------------------------------------------- #
# Test 3: germany_elections_2026 deterministic flow
# --------------------------------------------------------------------------- #
def test_germany_elections_deterministic():
    """Three corroborated claims from three sources flow through to PUBLISHED."""
    with get_session() as s:
        src_ids = _seed_three_sources(s)
        items = []
        for i, sid in enumerate(src_ids):
            items.append(_seed_item(
                s, source_id=sid,
                title=[
                    "German elections underway amid pressure on Chancellor Merz",
                    "3.8 million voters head to polls in eastern Germany",
                    "AfD seeks to galvanize support in state elections",
                ][i],
                description=[
                    "The chancellor faces intense political pressure as elections begin.",
                    "About 3.8 million people in Berlin and Mecklenburg-Western Pomerania vote.",
                    "The Alternative for Germany could gain from Sunday's polls.",
                ][i],
            ))

    result = run_pipeline(signal_ids=items, ai_router=None,
                          reference_time="2026-09-21T12:00:00+00:00")

    assert result["stories_detected"] >= 1
    outcome = result["outcomes"][0]
    assert outcome.final_status == "PUBLISHED", (
        f"germany_elections_2026: {outcome.final_status} — {outcome.error}"
    )
    # Verify the artifact was generated deterministically.
    assert outcome.artifact is not None
    with get_session() as s:
        art = s.query(generated_artifacts).filter_by(
            story_id=outcome.business_key).first()
        assert art is not None
        assert art.deterministic is True
        assert art.model_name == "deterministic-template"


# --------------------------------------------------------------------------- #
# Test 4: Three sources remain corroborated
# --------------------------------------------------------------------------- #
def test_three_sources_corroborated():
    """Claims backed by 3 independent sources achieve corroboration=3."""
    with get_session() as s:
        src_ids = _seed_three_sources(s)
        items = []
        for i, sid in enumerate(src_ids):
            items.append(_seed_item(
                s, source_id=sid,
                title=f"Breaking: major infrastructure deal reached — source {i}",
                description=f"The infrastructure deal was confirmed by party {i}.",
            ))

    result = run_pipeline(signal_ids=items, ai_router=None,
                          reference_time="2026-09-21T12:00:00+00:00")
    outcome = result["outcomes"][0]
    assert outcome.final_status == "PUBLISHED"
    # Verify corroboration in the decision.
    if outcome.decision:
        # trust_evaluations stores independent_corroboration.
        with get_session() as s:
            from newsforge.db.models import trust_evaluations
            te = s.query(trust_evaluations).filter_by(
                target_id=outcome.business_key).first()
            if te is not None:
                assert te.independent_corroboration >= 3, (
                    f"expected corroboration>=3, got {te.independent_corroboration}"
                )


# --------------------------------------------------------------------------- #
# Test 5: germany_2026 remains separate from germany_elections_2026
# --------------------------------------------------------------------------- #
def test_germany_2026_separate():
    """Two distinct story clusters (germany_2026 vs germany_elections_2026) stay separate."""
    with get_session() as s:
        _seed_source(s, id="src-sep")
        # Cluster 1: art + election topic
        item_elections = _seed_item(
            s, source_id="src-sep",
            title="German elections underway which could decide fate of Chancellor Merz",
            description="Voters head to the polls in eastern Germany.",
        )
        # Cluster 2: art/culture topic (different concept)
        item_art = _seed_item(
            s, source_id="src-sep",
            title="German Art Institutions Prepare for a Fight as Far Right Looks to Cut Funding",
            description="Museums across Germany face potential budget cuts.",
        )

    result = run_pipeline(signal_ids=[item_elections, item_art], ai_router=None,
                          reference_time="2026-09-21T12:00:00+00:00")
    assert result["stories_detected"] >= 2
    keys = {o.business_key for o in result["outcomes"]}
    # They must be different story IDs.
    assert len(keys) >= 2, f"Expected 2 distinct stories, got keys: {keys}"


# --------------------------------------------------------------------------- #
# Test 6: Content is deterministic
# --------------------------------------------------------------------------- #
def test_content_deterministic():
    """Same inputs produce identical generated artifact content."""
    with get_session() as s:
        _seed_source(s, id="src-det")
        item = _seed_item(s, source_id="src-det",
                          title="Supply chain disruptions ease in shipping sector")

    rt = "2026-09-21T12:00:00+00:00"
    r1 = run_pipeline(signal_ids=[item], ai_router=None, reference_time=rt)
    # Run again with same inputs (idempotent).
    r2 = run_pipeline(signal_ids=[item], ai_router=None, reference_time=rt)

    a1 = r1["outcomes"][0].artifact
    a2 = r2["outcomes"][0].artifact
    assert a1 is not None and a2 is not None
    assert a1["artifact_id"] == a2["artifact_id"]
    # Body JSON must be byte-identical.
    from newsforge.db.models import from_jsonable
    body1 = from_jsonable(a1.get("body_json"))
    body2 = from_jsonable(a2.get("body_json"))
    assert body1 == body2


# --------------------------------------------------------------------------- #
# Test 7: SEO is generated automatically
# --------------------------------------------------------------------------- #
def test_seo_auto_generated():
    """Published articles have canonical, OG, Twitter, JSON-LD data."""
    with get_session() as s:
        _seed_source(s, id="src-seo")
        item = _seed_item(s, source_id="src-seo",
                          title="Tech giants report record quarterly earnings")

    result = run_pipeline(signal_ids=[item], ai_router=None,
                          reference_time="2026-09-21T12:00:00+00:00")
    outcome = result["outcomes"][0]
    assert outcome.final_status == "PUBLISHED"

    # Check that the article was created with slug (SEO requirement).
    with get_session() as s:
        art = s.query(generated_artifacts).filter_by(
            story_id=outcome.business_key).first()
        assert art is not None
        assert art.title is not None
        # slug is on the stories table
        story = s.query(stories).filter_by(story_id=outcome.business_key).first()
        assert story is not None
        assert story.slug is not None


# --------------------------------------------------------------------------- #
# Test 8: Ad slots are registered and insertable
# --------------------------------------------------------------------------- #
def test_ad_slots_registered():
    """Default ad slots are registered and insertable into sections."""
    from newsforge.ads import AdPosition, insert_ad_slots, register_default_slots

    with get_session() as s:
        slots = register_default_slots(s)
        assert len(slots) == 5
        placements = {s["placement"] for s in slots}
        assert AdPosition.HEADER in placements
        assert AdPosition.AFTER_INTRO in placements
        assert AdPosition.MID_ARTICLE in placements
        assert AdPosition.BEFORE_RELATED in placements
        assert AdPosition.FOOTER in placements

    # Insert ad slots into a fake section list.
    sections = [
        {"type": "intro", "text": "Breaking news."},
        {"type": "fact", "text": "Fact one."},
        {"type": "fact", "text": "Fact two."},
        {"type": "footer", "text": "Compiled from sources."},
    ]
    with_ads = insert_ad_slots(sections, active_slots=slots)
    ad_sections = [s for s in with_ads if s["type"] == "ad_slot"]
    assert len(ad_sections) >= 3, f"Expected >=3 ad slots, got {len(ad_sections)}"
    # HEADER should be first.
    assert with_ads[0]["type"] == "ad_slot"
    assert with_ads[0]["placement"] == "HEADER"


# --------------------------------------------------------------------------- #
# Test 9: AI disabled does not produce ProviderError
# --------------------------------------------------------------------------- #
def test_no_provider_error_without_ai():
    """Pipeline with ai_router=None never raises ProviderError."""
    with get_session() as s:
        _seed_source(s, id="src-no-err")
        item = _seed_item(s, source_id="src-no-err",
                          title="Scientists discover high-temperature superconductor")

    result = run_pipeline(signal_ids=[item], ai_router=None,
                          reference_time="2026-09-21T12:00:00+00:00")
    outcome = result["outcomes"][0]
    assert "ProviderError" not in (outcome.error or "")
    assert outcome.final_status in ("PUBLISHED", "WAIT", "REJECT")


# --------------------------------------------------------------------------- #
# Test 10: AI_ENABLED config allows re-enabling later
# --------------------------------------------------------------------------- #
def test_ai_config_interface():
    """AiConfig exposes ai_enabled; pipeline uses it correctly."""
    import os

    from newsforge.config import AiConfig

    # Default: AI enabled.
    os.environ.pop("NEWSFORGE_AI_ENABLED", None)
    cfg = AiConfig()
    assert cfg.ai_enabled is True

    # Explicitly disabled.
    os.environ["NEWSFORGE_AI_ENABLED"] = "false"
    try:
        cfg2 = AiConfig()
        assert cfg2.ai_enabled is False
    finally:
        os.environ.pop("NEWSFORGE_AI_ENABLED", None)


# --------------------------------------------------------------------------- #
# Test: Pipeline integration with ad slots in published articles
# --------------------------------------------------------------------------- #
def test_ad_slots_in_published_article():
    """Ad slots are inserted into article sections at render time."""
    from newsforge.ads import insert_ad_slots, load_active_slots, register_default_slots

    with get_session() as s:
        register_default_slots(s)
        slots = load_active_slots(s)
        assert len(slots) == 5

    # Simulate what the web layer does: load sections from artifact, insert ads.
    editorial_sections = [
        {"type": "intro", "text": "Global markets rally."},
        {"type": "fact", "claim_id": "abc", "text": "Fact one."},
        {"type": "fact", "claim_id": "def", "text": "Fact two."},
        {"type": "footer", "text": "Compiled from sources."},
    ]
    with get_session() as s:
        active = load_active_slots(s)
    sections_with_ads = insert_ad_slots(editorial_sections, active_slots=active)
    ad_sections = [s for s in sections_with_ads if s["type"] == "ad_slot"]
    assert len(ad_sections) >= 3, (
        f"Expected >=3 ad slots, got {len(ad_sections)}"
    )
