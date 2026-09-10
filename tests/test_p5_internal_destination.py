"""P5 — real internal persistent destination tests.

``internal`` turns an approved publication into a persistent ``articles`` row readable at
``/articles/{slug}``. Tests drive the REAL publisher against an isolated database (no
mocks for the destination itself):

* publication materialises a PUBLISHED article row (idempotent, never duplicated);
* the article is served by the web layer at ``/articles/{slug}``;
* REVIEW / WAIT / REJECT verdicts produce NO article row;
* a failure is isolated to the channel and does not corrupt editorial state;
* the publish-time snapshot + destination metrics are recorded (P5/P6 chain);
* full E2E via the orchestrator: source -> story -> decision -> AI(MOCK) -> internal
  publish -> snapshot -> measurement -> article read.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import newsforge.db as db
from newsforge.db import (
    get_session,
    decisions,
    generated_artifacts,
    publications,
    publication_attempts,
    published_snapshots,
    destination_metrics,
    stories,
    use_isolated_database_ctx,
)
from newsforge.db.models import ArticleStatus
from newsforge.publish import publish_story
from newsforge.pipeline.orchestrator import run_pipeline
from newsforge.web.app import create_app

_OUTLET = "2026-09-05T12:00:00+00:00"


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
    with use_isolated_database_ctx(path):
        yield


@pytest.fixture(autouse=True)
def clean_registry():
    from newsforge.publish.destinations import register_builtin_destinations, reset_registry
    reset_registry()
    register_builtin_destinations()
    yield
    reset_registry()


# --------------------------------------------------------------------------- #
# Seeding helpers (REAL rows; decisions seeded via the persisted-verdict API)
# --------------------------------------------------------------------------- #
def _seed_story(session, *, story_id="story-1", title="Test Story",
                summary="A story about a tax change.", slug="story-1"):
    st = stories()
    st.id = story_id
    st.story_id = story_id
    st.slug = slug
    st.title = title
    st.summary = summary
    session.add(st)
    session.commit()
    return story_id


def _seed_decision(session, *, story_id="story-1", decision="PUBLISH",
                   human_override=False, risk_level=None):
    row = decisions(
        target_type="STORY",
        target_id=story_id,
        decision=decision,
        human_override=human_override,
        risk_level=risk_level,
    )
    session.add(row)
    session.commit()
    return str(row.id)


def _seed_publishable_story(story_id="story-1"):
    """Story + PUBLISH decision + generated article (no real network, MOCK AI)."""
    with get_session() as s:
        _seed_story(s, story_id=story_id)
        _seed_decision(s, story_id=story_id, decision="PUBLISH")
        from newsforge.db.models import ArtifactFormat, GenerationState
        from newsforge.generate.assembly import generate_story

        gen = generate_story(s, story_id=story_id, format=ArtifactFormat.ARTICLE.value,
                             reference_time=_OUTLET)
    assert gen["state"] == GenerationState.VALIDATED.value and gen["publishable"] is True
    return gen


def _client():
    return TestClient(create_app())


# --------------------------------------------------------------------------- #
# 1. Real persistent destination: materialises a readable PUBLISHED article
# --------------------------------------------------------------------------- #
def test_internal_destination_creates_persistent_article():
    _seed_publishable_story()

    with get_session() as s:
        result = publish_story(s, story_id="story-1", destinations=["internal"])
    assert result["blocked"] is False and result["published"] is True

    with get_session() as s:
        pubs = s.query(publications).filter_by(story_id="story-1").all()
        assert len(pubs) == 1
        assert str(pubs[0].status) == "COMPLETED"
        assert pubs[0].destination_key == "internal"

        art = s.query(db.articles).filter_by(story_id="story-1").one()
        assert art.slug == "story-1"
        assert art.title == "Test Story"
        assert str(art.status) == ArticleStatus.PUBLISHED.value
        assert art.body_html and "A story about a tax change." in art.body_html

    # Readable at /articles/{slug} through the real web layer.
    r = _client().get("/articles/story-1")
    assert r.status_code == 200
    assert "Test Story" in r.text
    assert "A story about a tax change." in r.text


# --------------------------------------------------------------------------- #
# 2. Idempotency: re-publish never duplicates the article row
# --------------------------------------------------------------------------- #
def test_internal_destination_is_idempotent():
    _seed_publishable_story()
    with get_session() as s:
        publish_story(s, story_id="story-1", destinations=["internal"])

    with get_session() as s:
        first_article_id = str(s.query(db.articles).filter_by(story_id="story-1").one().id)
        first_pub_id = str(s.query(publications).filter_by(story_id="story-1").one().id)

    with get_session() as s:
        result = publish_story(s, story_id="story-1", destinations=["internal"])
    assert result["published"] is True

    with get_session() as s:
        arts = s.query(db.articles).filter_by(story_id="story-1").all()
        pubs = s.query(publications).filter_by(story_id="story-1").all()
        attempts = s.query(publication_attempts).all()
    assert len(arts) == 1, "exactly one article row across re-publishes"
    assert len(pubs) == 1, "publication row stable"
    assert len(attempts) == 1, "no duplicate attempt rows"
    assert str(arts[0].id) == first_article_id


# --------------------------------------------------------------------------- #
# 3. Gate: REVIEW / WAIT / REJECT -> NO article is ever materialised
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("decision", ["REVIEW", "WAIT", "REJECT", "DRAFT"])
def test_non_publish_verdicts_create_no_article(decision):
    with get_session() as s:
        _seed_story(s)
        _seed_decision(s, decision=decision)
        result = publish_story(s, story_id="story-1", destinations=["internal"])
    assert result["blocked"] is True and result["published"] is False
    with get_session() as s:
        assert s.query(publications).count() == 0
        assert s.query(db.articles).count() == 0


# --------------------------------------------------------------------------- #
# 4. Failure isolation: unknown story -> FAILED attempt, no crash, no article
# --------------------------------------------------------------------------- #
def test_internal_destination_failure_is_isolated():
    with get_session() as s:
        _seed_publishable_story()
        result = publish_story(s, story_id="story-1", destinations=["internal", "recording"])
    assert result["published"] is True  # recording still succeeded
    assert result["per_destination"]["internal"]["succeeded"] is True

    # Point the destination at a story that does not exist: the channel fails cleanly.
    from newsforge.publish.destinations import get_destination

    dest = get_destination("internal")
    outcome = dest.publish(payload={"story_id": "does-not-exist"})
    assert outcome.ok is False
    assert "not found" in (outcome.error or "")


# --------------------------------------------------------------------------- #
# 5. Boundary: publishing never mutates editorial state (story / decision)
# --------------------------------------------------------------------------- #
def test_internal_destination_preserves_editorial_state():
    _seed_publishable_story()
    with get_session() as s:
        story = s.query(stories).filter_by(id="story-1").one()
        decision = s.query(decisions).filter_by(target_type="STORY", target_id="story-1").one()
    before = (story.title, story.summary, story.slug, str(story.status),
              decision.decision, bool(decision.human_override))

    with get_session() as s:
        publish_story(s, story_id="story-1", destinations=["internal"])

    with get_session() as s:
        story = s.query(stories).filter_by(id="story-1").one()
        decision = s.query(decisions).filter_by(target_type="STORY", target_id="story-1").one()
    after = (story.title, story.summary, story.slug, str(story.status),
             decision.decision, bool(decision.human_override))
    assert before == after, "publishing must never mutate Story/Decision rows"


# --------------------------------------------------------------------------- #
# 6. Measurement chain: snapshot + destination metrics are recorded
# --------------------------------------------------------------------------- #
def test_internal_destination_feeds_snapshot_and_metrics():
    _seed_publishable_story()
    with get_session() as s:
        publish_story(s, story_id="story-1", destinations=["internal"])

        from newsforge.measurement import record_destination_metrics

        metrics = record_destination_metrics(s, story_id="story-1", reference_time=_OUTLET)

    with get_session() as s:
        snaps = s.query(published_snapshots).filter_by(story_id="story-1").all()
        rows = s.query(destination_metrics).filter_by(story_id="story-1").all()
    assert len(snaps) >= 1, "publish snapshot must exist"
    assert snaps[0].slug == "story-1"
    assert len(rows) >= 1
    assert any(str(r.destination_key) == "internal" for r in rows)
    # The article row now marks the content PUBLISHED — the measurement chain is fed
    # by the same persisted verdict consumed by the publisher.
    assert any(r["n_attempts"] >= 1 for r in metrics["destinations"])


# --------------------------------------------------------------------------- #
# 7. Full E2E (MOCK AI, real internal destination, real web read)
# --------------------------------------------------------------------------- #
def test_end_to_end_source_to_article_read():
    """source -> story -> decision -> AI(MOCK) -> publish(internal) -> snapshot ->
    measurement -> article readable at /articles/{slug}."""
    with get_session() as s:
        s.add(db.sources(id="src-e2e", source_id="src-e2e", name="E2E Official",
                         type="RSS", country="ES", language="es", tier="TIER_1",
                         trust_score=90, status="active"))
        item = db.source_items(
            source_id="src-e2e",
            title="The income tax rose by three percent.",
            content_text="The income tax rose by three percent.",
            published_at="2026-09-05T10:00:00+00:00",
            dedupe_hash="hash-e2e-internal",
        )
        s.add(item)
        s.commit()
        item_id = str(item.id)

    result = run_pipeline(signal_ids=[item_id], reference_time=_OUTLET,
                          destinations=["internal"])
    assert result["status"] == "ok"
    outcome = result["outcomes"][0]
    assert outcome.final_status == "PUBLISHED", outcome.error
    assert outcome.artifact is not None and outcome.publish is not None
    assert outcome.measurement is not None

    with get_session() as s:
        arts = s.query(db.articles).all()
        assert len(arts) == 1, "one persistent article for the published story"
        art = arts[0]
        assert str(art.status) == ArticleStatus.PUBLISHED.value
        assert s.query(published_snapshots).filter_by(story_id=art.story_id).count() >= 1
        assert s.query(destination_metrics).filter_by(story_id=art.story_id).count() >= 1

        story = s.query(stories).filter_by(id=art.story_id).one()
        slug = art.slug or story.slug or story.story_id

    r = _client().get(f"/articles/{slug}")
    assert r.status_code == 200
    assert "income tax" in r.text.lower()
    # The article is included in the public index + sitemap + feed.
    assert f"/articles/{slug}" in _client().get("/articles").text
    assert f"/articles/{slug}" in _client().get("/sitemap.xml").text
    assert f"/articles/{slug}" in _client().get("/feed.xml").text