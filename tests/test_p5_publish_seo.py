"""P5 — SEO + CMS/Publish gate tests (real execution, deterministic, offline).

Every test runs against a throwaway SQLite database (isolated per test) with its own session.
No network, no LLM provider: the P3 verification flow (run_verification), the P4 generator
(generate_story) and the publisher are all local and deterministic (§15).

Covered gate areas:
  * publication + persistence (COMPLETED row, published_at, idempotency_key)
  * publish-time snapshot (published_snapshots persisted by the publish flow)
  * publish gate (no decision / non-PUBLISH verdict / human_override -> blocked)
  * idempotency (publish(artifact) twice -> one logical publication, stable id)
  * rejection of a non-publishable artifact (publishable=False never publishes)
  * web SSR: GET /articles, GET /articles/{slug}, unpublished -> 404
  * SEO markup in the REAL rendered HTML: JSON-LD (valid), canonical, OpenGraph, Twitter cards
  * sitemap.xml and feed.xml: valid XML containing the published article

Run: python -m pytest tests/test_p5_publish_seo.py -q
"""
from __future__ import annotations

import email.utils
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from newsforge.config import BrandConfig
from newsforge.db import (
    decisions,
    generated_artifacts,
    get_session,
    publication_attempts,
    publications,
    published_snapshots,
    stories,
    use_isolated_database_ctx,
)
from newsforge.db.models import ArtifactFormat, GenerationState, from_jsonable
from newsforge.generate import generate_story
from newsforge.publish import idempotency_key, publish_story, register_builtin_destinations, reset_registry
from newsforge.verify.persist import run_verification
from newsforge.web.app import create_app

_db_seq = 0

T = "2026-09-06T10:00:00+00:00"          # injected clock for reproducible runs
PUBLISHED_AT = "2026-09-07T00:00:00+00:00"  # fixed timestamp of the built-in recording destination


@pytest.fixture(autouse=True)
def isolated_db():
    """Give every test its own throwaway database so no row can leak between tests."""
    global _db_seq
    db_dir = Path.cwd() / ".pytest_tmp"
    path = db_dir / f"p5seo_{_db_seq}.db"
    try:
        if path.exists():
            path.unlink()
    except OSError:
        _db_seq += 1
        path = db_dir / f"p5seo_{_db_seq}.db"
    db_dir.mkdir(exist_ok=True)
    with use_isolated_database_ctx(path):
        yield
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def clean_registry():
    """Start every test from a pristine destination registry (built-ins only)."""
    reset_registry()
    register_builtin_destinations()
    yield


# --------------------------------------------------------------------------- #
# Seeding helpers — the REAL P3 flow (run_verification), never invented decisions
# --------------------------------------------------------------------------- #
def _seed_item(session, *, title, source_id, published_at="2026-09-05T10:00:00+00:00"):
    from newsforge.db import source_items

    item = source_items()
    item.source_id = source_id
    item.title = title
    item.content_text = title
    item.published_at = published_at
    item.dedupe_hash = f"hash-{title}"
    session.add(item)
    session.commit()
    return str(item.id)


def _seed_story(session, *, story_id="story-1", title="Test Story",
                summary="A story about a tax change."):
    st = stories()
    st.id = story_id
    st.story_id = story_id
    st.slug = story_id
    st.title = title
    st.summary = summary
    session.add(st)
    session.commit()
    return story_id


def _seed_story_with_claims(session, *, story_id="story-1", claim_specs=None):
    """Seed source(s) + item(s) + story and run the REAL P3 verification pipeline."""
    from newsforge.db import sources as _sources

    seen_sources = set()
    for spec in claim_specs:
        sid = spec["source_id"]
        if sid in seen_sources:
            continue
        seen_sources.add(sid)
        s = _sources()
        s.id = sid
        s.source_id = sid
        s.name = f"Source {sid}"
        s.type = "RSS"
        s.country = "ES"
        s.language = "es"
        s.tier = spec.get("tier", "TIER_1")
        s.trust_score = 90 if spec.get("tier", "TIER_1") == "TIER_1" else 35
        s.status = "active"
        session.add(s)

    items = {}
    for spec in claim_specs:
        sid = spec["source_id"]
        item = _seed_item(session, title=f"item-{sid}", source_id=sid)
        items[sid] = item
    session.commit()

    _seed_story(session, story_id=story_id)

    specs = []
    for spec in claim_specs:
        entry = {
            "claim_id": spec["claim_id"],
            "text": spec["text"],
            "story_id": story_id,
            "tiers": [spec.get("tier", "TIER_1")],
        }
        if spec.get("with_evidence", True):
            entry["source_item_ids"] = [items[spec["source_id"]]]
        specs.append(entry)

    return run_verification(claims_specs=specs, story_id=story_id, reference_time=T)


def _seed_decision(session, *, story_id="story-1", decision="PUBLISH", human_override=False):
    """Seed a persisted Decision Engine verdict for the STORY (gate tests only)."""
    row = decisions(
        target_type="STORY",
        target_id=story_id,
        decision=decision,
        human_override=human_override,
    )
    session.add(row)
    session.commit()
    return str(row.id)


def _e2e_published(story_id="story-1"):
    """Full P3 -> P4 -> P5 flow: verify (PUBLISH) -> generate artifact -> publish.

    Publishes explicitly to the ``recording`` destination so this suite's single-
    publication/single-attempt assertions stay valid. The REAL persistent ``internal``
    destination is covered by tests/test_p5_internal_destination.py."""
    with get_session() as s:
        result = _seed_story_with_claims(s, story_id=story_id, claim_specs=[
            {"claim_id": "c-main", "text": "The tax is three euros.", "source_id": "src-1"},
        ])
    assert result["decision"] == "PUBLISH"
    with get_session() as s:
        gen = generate_story(s, story_id=story_id, format=ArtifactFormat.ARTICLE.value, reference_time=T)
    assert gen["state"] == GenerationState.VALIDATED.value and gen["publishable"] is True
    with get_session() as s:
        pub = publish_story(s, story_id=story_id, destinations=["recording"])
    assert pub["blocked"] is False and pub["published"] is True
    return gen


def _client():
    return TestClient(create_app())


def _article_html(slug="story-1") -> str:
    r = _client().get(f"/articles/{slug}")
    assert r.status_code == 200
    return r.text


# --------------------------------------------------------------------------- #
# 1. publication: the pipeline article publishes and persists
# --------------------------------------------------------------------------- #
def test_pipeline_article_publishes_and_persists():
    _e2e_published()
    with get_session() as s:
        pub = s.query(publications).filter_by(story_id="story-1").one()
        assert str(pub.status) == "COMPLETED"
        # published_at is persisted from the confirmed SUCCEEDED attempt (no new facts).
        assert pub.published_at == PUBLISHED_AT
        # Stable idempotency key: deterministic function of (story, destination).
        assert pub.idempotency_key == idempotency_key("story-1", "recording")
        attempts = s.query(publication_attempts).filter_by(publication_id=str(pub.id)).all()
        assert len(attempts) == 1 and attempts[0].status == "SUCCEEDED"


# --------------------------------------------------------------------------- #
# 2. idempotency: publish(artifact) twice -> one logical publication
# --------------------------------------------------------------------------- #
def test_publish_is_idempotent():
    _e2e_published()  # first publish already happened inside the helper

    with get_session() as s:
        first = [str(p.id) for p in s.query(publications).filter_by(story_id="story-1").all()]
    assert len(first) == 1

    # Second publish of the SAME logical artifact (fresh session, fresh process view).
    with get_session() as s:
        pub2 = publish_story(s, story_id="story-1", destinations=["recording"])

    with get_session() as s:
        pubs = s.query(publications).filter_by(story_id="story-1").all()
        attempts = s.query(publication_attempts).all()
    # PUBLICATION_ID_STABLE=true / NO_DUPLICATE_PUBLICATION=true
    assert [str(p.id) for p in pubs] == first, "publication id must stay stable"
    assert len(pubs) == 1, "no duplicate publication rows"
    assert len(attempts) == 1, "no duplicate attempt rows"


# --------------------------------------------------------------------------- #
# 3. snapshot: persisted by the publish flow, idempotent across re-publishes
# --------------------------------------------------------------------------- #
def test_snapshot_persisted_on_publish():
    _e2e_published()
    with get_session() as s:
        snaps = s.query(published_snapshots).filter_by(story_id="story-1").all()
        assert len(snaps) == 1
        snap = snaps[0]
        story = s.query(stories).filter_by(id="story-1").one()
        pub = s.query(publications).filter_by(story_id="story-1").one()
        # Snapshot captures the EDITORIAL state at publication time (read-only capture).
        assert snap.title == story.title and snap.slug == story.slug
        assert snap.published_at == pub.published_at == PUBLISHED_AT

    # Re-publish must not duplicate the snapshot.
    with get_session() as s:
        publish_story(s, story_id="story-1")
    with get_session() as s:
        assert len(s.query(published_snapshots).filter_by(story_id="story-1").all()) == 1


# --------------------------------------------------------------------------- #
# 4. publish gate: nothing without a verified PUBLISH decision
# --------------------------------------------------------------------------- #
def test_publish_blocked_without_decision():
    with get_session() as s:
        _seed_story(s)
        result = publish_story(s, story_id="story-1")
    assert result["blocked"] is True and result["published"] is False
    assert "no persisted decision" in result["reason"]
    with get_session() as s:
        assert s.query(publications).count() == 0


@pytest.mark.parametrize("decision", ["REVIEW", "WAIT", "DRAFT", "REJECT"])
def test_publish_blocked_for_non_publish_verdict(decision):
    with get_session() as s:
        _seed_story(s)
        _seed_decision(s, decision=decision)
        result = publish_story(s, story_id="story-1")
    assert result["blocked"] is True and result["published"] is False
    with get_session() as s:
        assert s.query(publications).count() == 0


def test_publish_blocked_with_human_override():
    with get_session() as s:
        _seed_story(s)
        _seed_decision(s, decision="PUBLISH", human_override=True)
        result = publish_story(s, story_id="story-1")
    assert result["blocked"] is True and "human_override" in result["reason"]
    with get_session() as s:
        assert s.query(publications).count() == 0


# --------------------------------------------------------------------------- #
# 5. rejection of a non-publishable artifact
# --------------------------------------------------------------------------- #
def test_unpublishable_artifact_never_published():
    """A WAIT decision -> the generated artifact is publishable=False and the publisher blocks."""
    with get_session() as s:
        _seed_story(s)
        _seed_decision(s, decision="WAIT")
        gen = generate_story(s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T)
    assert gen["publishable"] is False

    with get_session() as s:
        result = publish_story(s, story_id="story-1")
    assert result["blocked"] is True and result["published"] is False
    with get_session() as s:
        assert s.query(publications).count() == 0
        # The artifact exists but must never surface publicly.
        assert s.query(generated_artifacts).filter_by(story_id="story-1").count() == 1


# --------------------------------------------------------------------------- #
# 6. web SSR: /articles and the individual article page (real HTTP)
# --------------------------------------------------------------------------- #
def test_articles_index_lists_published_article():
    _e2e_published()
    r = _client().get("/articles")
    assert r.status_code == 200
    assert f"/articles/story-1" in r.text
    assert "Test Story" in r.text


def test_article_page_renders_persisted_artifact_body():
    _e2e_published()
    html = _article_html("story-1")
    assert "<h1>Test Story</h1>" in html
    # The body comes from the persisted GeneratedArtifact (the sourced claim), not new facts.
    assert "The tax is three euros." in html


def test_unpublished_article_is_never_rendered():
    """Story with a non-publishable artifact and no publication -> 404, absent everywhere."""
    with get_session() as s:
        _seed_story(s)
        _seed_decision(s, decision="WAIT")
        generate_story(s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T)

    r = _client().get("/articles/story-1")
    assert r.status_code == 404
    idx = _client().get("/articles").text
    assert "/articles/story-1" not in idx
    sitemap = _client().get("/sitemap.xml").text
    assert "story-1" not in sitemap
    feed = _client().get("/feed.xml").text
    assert "story-1" not in feed


def test_unknown_article_is_404():
    assert _client().get("/articles/does-not-exist").status_code == 404


# --------------------------------------------------------------------------- #
# 7. SEO markup in the REAL rendered HTML
# --------------------------------------------------------------------------- #
def test_jsonld_present_and_valid():
    _e2e_published()
    html = _article_html("story-1")
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)         or re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    assert m is not None, "JSON-LD script tag must be present in the rendered HTML"
    doc = json.loads(m.group(1))  # JSON_LD_VALID=true
    assert doc["@type"] == "NewsArticle"
    brand = BrandConfig()
    url = f"{brand.site_url}/articles/story-1"
    assert doc["headline"] == "Test Story"
    assert doc["url"] == url and doc["mainEntityOfPage"] == url
    assert doc["datePublished"] == PUBLISHED_AT


def test_canonical_present_and_correct():
    _e2e_published()
    html = _article_html("story-1")
    m = re.search(r'<link rel="canonical" href="([^"]+)"', html)
    assert m is not None, "canonical link must be present in the rendered HTML"
    brand = BrandConfig()
    assert m.group(1) == f"{brand.site_url}/articles/story-1"


def test_open_graph_tags_present():
    _e2e_published()
    html = _article_html("story-1")
    brand = BrandConfig()
    url = f"{brand.site_url}/articles/story-1"
    assert 'property="og:type" content="article"' in html
    assert f'property="og:site_name" content="{brand.name}"' in html
    assert 'property="og:title" content="Test Story"' in html
    assert f'property="og:url" content="{url}"' in html


def test_twitter_card_tags_present():
    _e2e_published()
    html = _article_html("story-1")
    assert 'name="twitter:card" content="summary"' in html
    assert 'name="twitter:title" content="Test Story"' in html


# --------------------------------------------------------------------------- #
# 8. sitemap + RSS: valid XML containing the published article
# --------------------------------------------------------------------------- #
def test_sitemap_contains_published_article():
    _e2e_published()
    r = _client().get("/sitemap.xml")
    assert r.status_code == 200
    root = ET.fromstring(r.text)  # valid XML
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    locs = [loc.text for loc in root.findall(f".//{ns}loc")]
    brand = BrandConfig()
    assert f"{brand.site_url}/articles/story-1" in locs


def test_rss_feed_contains_published_article_and_is_valid_xml():
    _e2e_published()
    r = _client().get("/feed.xml")
    assert r.status_code == 200
    root = ET.fromstring(r.text)  # valid XML (RSS 2.0)
    assert root.tag == "rss" and root.get("version") == "2.0"
    items = root.findall(".//item")
    assert len(items) == 1
    item = items[0]
    brand = BrandConfig()
    url = f"{brand.site_url}/articles/story-1"
    assert item.findtext("title") == "Test Story"
    assert item.findtext("link") == url
    pubdate = item.findtext("pubDate")
    assert pubdate, "RSS item must carry a pubDate"
    email.utils.parsedate_to_datetime(pubdate)  # raises if not RFC 822


# --------------------------------------------------------------------------- #
# 9. boundary safety: publishing never mutates editorial state (no new facts)
# --------------------------------------------------------------------------- #
def test_publish_does_not_mutate_editorial_state():
    _e2e_published()
    with get_session() as s:
        story = s.query(stories).filter_by(id="story-1").one()
        decision = s.query(decisions).filter_by(target_type="STORY", target_id="story-1").one()
    before = (story.title, story.summary, story.slug, str(story.status),
              decision.decision, bool(decision.human_override))

    with get_session() as s:
        publish_story(s, story_id="story-1")  # re-publish

    with get_session() as s:
        story = s.query(stories).filter_by(id="story-1").one()
        decision = s.query(decisions).filter_by(target_type="STORY", target_id="story-1").one()
    after = (story.title, story.summary, story.slug, str(story.status),
             decision.decision, bool(decision.human_override))
    assert before == after, "publishing must never mutate Story/Decision rows"
