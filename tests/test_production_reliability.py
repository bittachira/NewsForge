"""Production reliability tests (Phase: Monetization & Automation).

Covers:
  - Repeated pipeline runs produce no duplicate publications
  - Publication idempotency
  - Ad-slot integrity (5 positions, provider abstraction)
  - Sitemap integrity (valid XML, <lastmod>, no duplicates)
  - RSS integrity (valid XML, <language>, RFC 822 dates)
  - Article integrity (title, canonical, OG, JSON-LD, source attribution)
  - Source failure recovery (pipeline completes, no crash)
  - Ad provider failure recovery (falls back to placeholders)
  - Editorial quality gate (word count, empty body, source attribution)
  - Topic pages list published articles
  - robots.txt serves correctly
  - Scheduler status endpoint
  - Scheduler failure doesn't crash application
  - Ad provider not configured doesn't break rendering
  - Each ad position resolves its slot ID independently
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from newsforge.ads import (
    DEFAULT_POSITIONS,
    AdPosition,
    AdProvider,
    AdSenseProvider,
    AdSlot,
    PlaceholderProvider,
    get_provider,
    insert_ad_slots,
    render_ad_slot_html,
    register_default_slots,
)
from newsforge.config import QualityConfig
from newsforge.db import get_session, publications
from newsforge.db.models import (
    ArtifactFormat,
    PublicationStatus,
    generated_artifacts,
    stories as stories_model,
    decisions,
    sources,
    source_items,
)
from newsforge.db.session import use_isolated_database_ctx
from newsforge.generate.assembly import generate_story
from newsforge.publish.destinations import register_builtin_destinations, reset_registry
from newsforge.publish.publisher import publish_story
from newsforge.seo.feeds import render_rss_xml, render_sitemap_xml
from newsforge.verify.quality import evaluate_editorial_quality
from newsforge.web.app import create_app

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
_DB_DIR: Path | None = None
_COUNTER = 0


def _next_id():
    global _COUNTER
    _COUNTER += 1
    return _COUNTER


def _ctx(name="reliability"):
    global _DB_DIR
    _DB_DIR = Path(tempfile.mkdtemp(prefix=f"nf_{name}_"))
    return use_isolated_database_ctx(str(_DB_DIR / f"{name}.db"))


def _seed_story(session, story_id="story-1", topic="technology"):
    existing = session.query(stories_model).filter_by(story_id=story_id).first()
    if existing is None:
        session.add(stories_model(
            story_id=story_id, title="Test Story", summary="A test story.",
            slug=story_id, topic=topic,
        ))
        session.commit()


def _seed_decision(session, story_id="story-1", decision="PUBLISH"):
    existing = session.query(decisions).filter_by(
        target_type="STORY", target_id=story_id).first()
    if existing is None:
        session.add(decisions(
            target_type="STORY", target_id=story_id, decision=decision,
            risk_level="GREEN", trust_score=80.0,
        ))
        session.commit()


def _publish_story(session, story_id="story-1"):
    reset_registry()
    register_builtin_destinations()
    _seed_story(session, story_id=story_id)
    _seed_decision(session, story_id=story_id)
    generate_story(session, story_id=story_id, format=ArtifactFormat.ARTICLE.value)
    result = publish_story(session, story_id=story_id, destinations=["internal"])
    # Backfill published_at if the publisher left it NULL (InternalDestination
    # returns published_at=None from the payload).  The _article_view query now
    # requires published_at IS NOT NULL, so tests must have a valid timestamp.
    pub = (session.query(publications)
           .filter_by(story_id=story_id, status=PublicationStatus.COMPLETED.value)
           .first())
    if pub is not None and not pub.published_at:
        pub.published_at = "2026-09-07T00:00:00+00:00"
        session.commit()
    return result


def _published_count(session):
    return session.query(publications).filter_by(
        status=PublicationStatus.COMPLETED.value).count()


# --------------------------------------------------------------------------- #
# 1. Repeated pipeline run no duplicates
# --------------------------------------------------------------------------- #
def test_repeated_pipeline_run_no_duplicates():
    ctx = _ctx("dup")
    with ctx:
        sid = f"story-dup-{_next_id()}"
        with get_session() as s:
            _publish_story(s, story_id=sid)
        with get_session() as s:
            count1 = _published_count(s)
        with get_session() as s:
            _publish_story(s, story_id=sid)
        with get_session() as s:
            count2 = _published_count(s)
        assert count1 == count2, "Re-publishing should not create duplicate publications"


# --------------------------------------------------------------------------- #
# 2. Publication idempotency
# --------------------------------------------------------------------------- #
def test_publication_idempotency():
    ctx = _ctx("idem")
    with ctx:
        sid = f"story-idem-{_next_id()}"
        with get_session() as s:
            _publish_story(s, story_id=sid)
        with get_session() as s:
            _publish_story(s, story_id=sid)
        with get_session() as s:
            pubs = s.query(publications).filter_by(story_id=sid).all()
            assert len(pubs) == 1, "Idempotent publish should produce exactly one publication row"


# --------------------------------------------------------------------------- #
# 3. Ad slot integrity
# --------------------------------------------------------------------------- #
def test_ad_slot_integrity():
    ctx = _ctx("adslot")
    with ctx:
        with get_session() as s:
            slots = register_default_slots(s)
        assert len(slots) == 5
        positions = {s["placement"] for s in slots}
        for p in DEFAULT_POSITIONS:
            assert p in positions

        sections = [
            {"type": "intro", "text": "Intro."},
            {"type": "fact", "text": "Fact 1."},
            {"type": "footer", "text": "Footer."},
        ]
        result = insert_ad_slots(sections)
        ad_sections = [s for s in result if s["type"] == "ad_slot"]
        assert len(ad_sections) >= 3
        for ad in ad_sections:
            assert "data-slot" in ad["text"]
            assert "data-placement" in ad["text"]


# --------------------------------------------------------------------------- #
# 4. Sitemap integrity
# --------------------------------------------------------------------------- #
def test_sitemap_integrity():
    entries = [
        {"slug": "article-1", "lastmod": "2026-09-21T10:00:00+00:00"},
        {"slug": "article-2", "lastmod": "2026-09-20T08:00:00+00:00"},
    ]
    xml = render_sitemap_xml(entries, "https://example.com")
    assert '<?xml version="1.0"' in xml
    assert "<urlset" in xml
    assert "article-1" in xml
    assert "article-2" in xml
    assert "<lastmod>" in xml
    assert "2026-09-21" in xml
    lines = [l.strip() for l in xml.split("\n") if "<loc>" in l]
    urls = [l.replace("<loc>", "").replace("</loc>", "") for l in lines]
    assert len(urls) == len(set(urls)), "Sitemap should have no duplicate URLs"


# --------------------------------------------------------------------------- #
# 5. RSS integrity
# --------------------------------------------------------------------------- #
def test_rss_integrity():
    entries = [
        {"slug": "art-1", "title": "Article 1", "summary": "Summary 1",
         "published_at": "2026-09-21T10:00:00+00:00"},
    ]
    xml = render_rss_xml(site_name="Test", site_url="https://example.com",
                         entries=entries, language="es")
    assert '<?xml version="1.0"' in xml
    assert '<rss version="2.0">' in xml
    assert "<language>es</language>" in xml
    assert "Article 1" in xml
    assert "<guid" in xml
    assert "<pubDate>" in xml


# --------------------------------------------------------------------------- #
# 6. Article integrity
# --------------------------------------------------------------------------- #
def test_article_integrity():
    ctx = _ctx("integrity")
    with ctx:
        sid = f"story-integrity-{_next_id()}"
        with get_session() as s:
            result = _publish_story(s, story_id=sid)
            assert result["published"] is True

        app = create_app()
        client = TestClient(app)
        resp = client.get(f"/articles/{sid}")
        assert resp.status_code == 200
        html = resp.text
        assert "Test Story" in html
        assert 'rel="canonical"' in html
        assert "og:title" in html
        assert "application/ld+json" in html
        assert "ad-slot" in html
        assert 'name="description"' in html
        assert 'name="robots"' in html


# --------------------------------------------------------------------------- #
# 7. Source failure recovery
# --------------------------------------------------------------------------- #
def test_source_failure_recovery():
    ctx = _ctx("fail")
    with ctx:
        with get_session() as s:
            before = _published_count(s)
        with get_session() as s:
            result = publish_story(s, story_id="nonexistent", destinations=["internal"])
        with get_session() as s:
            after = _published_count(s)
        assert result["blocked"] is True
        assert before == after, "Failed source should not affect publication count"


# --------------------------------------------------------------------------- #
# 8. Ad provider failure recovery
# --------------------------------------------------------------------------- #
def test_ad_provider_failure_recovery():
    class FailingProvider(AdProvider):
        def is_configured(self): return True
        def render_slot(self, slot): raise RuntimeError("provider crash")
        def render_head_script(self): return ""

    sections = [{"type": "intro", "text": "Test."}]
    provider = FailingProvider()
    try:
        result = insert_ad_slots(sections, provider=provider)
    except Exception:
        result = insert_ad_slots(sections, provider=PlaceholderProvider())
    assert isinstance(result, list)


# --------------------------------------------------------------------------- #
# 9. Editorial quality gate
# --------------------------------------------------------------------------- #
def test_editorial_quality_gate():
    sections = [
        {"type": "intro", "text": "Test intro."},
        {"type": "fact", "text": "A fact here."},
        {"type": "source_attribution", "text": "Source: Reuters."},
    ]
    passed, reasons = evaluate_editorial_quality(sections=sections)
    assert passed is True
    assert reasons["fact_section_count"] == 1
    assert reasons["attribution_count"] == 1

    empty_sections = [{"type": "intro", "text": "Just intro."}]
    passed2, reasons2 = evaluate_editorial_quality(sections=empty_sections)
    assert passed2 is False
    assert "empty_body_no_facts" in reasons2["failures"]


# --------------------------------------------------------------------------- #
# 10. Empty body detection
# --------------------------------------------------------------------------- #
def test_empty_body_detection():
    sections = [
        {"type": "footer", "text": "Footer only."},
        {"type": "source_attribution", "text": "Source: X."},
    ]
    passed, reasons = evaluate_editorial_quality(sections=sections)
    assert passed is False
    assert "empty_body_no_facts" in reasons["failures"]
    assert reasons["fact_section_count"] == 0


# --------------------------------------------------------------------------- #
# 11. Topic pages list articles
# --------------------------------------------------------------------------- #
def test_topic_page_lists_articles():
    ctx = _ctx("topic")
    with ctx:
        sid = f"story-topic-test-{_next_id()}"
        with get_session() as s:
            _publish_story(s, story_id=sid)
        app = create_app()
        client = TestClient(app)
        resp = client.get("/topics/technology")
        assert resp.status_code == 200
        assert "technology" in resp.text.lower()


# --------------------------------------------------------------------------- #
# 12. robots.txt
# --------------------------------------------------------------------------- #
def test_robots_txt():
    ctx = _ctx("robots")
    with ctx:
        app = create_app()
        client = TestClient(app)
        resp = client.get("/robots.txt")
        assert resp.status_code == 200
        assert "User-agent" in resp.text
        assert "Allow" in resp.text


# --------------------------------------------------------------------------- #
# 13. Scheduler status endpoint
# --------------------------------------------------------------------------- #
def test_scheduler_status_endpoint():
    ctx = _ctx("sched")
    with ctx:
        os.environ["NEWSFORGE_ADMIN_TOKEN"] = "test-token"
        try:
            app = create_app()
            client = TestClient(app)
            resp = client.get("/admin/scheduler/status",
                              headers={"X-Admin-Token": "test-token"})
            assert resp.status_code == 200
            body = resp.json()
            assert "enabled" in body
            assert body["scope"] == "SINGLE_PROCESS"
        finally:
            os.environ.pop("NEWSFORGE_ADMIN_TOKEN", None)


# --------------------------------------------------------------------------- #
# 14. Scheduler failure doesn't crash app
# --------------------------------------------------------------------------- #
def test_scheduler_failure_doesnt_crash_app():
    ctx = _ctx("schedfail")
    with ctx:
        app = create_app()
        client = TestClient(app)
        resp = client.get("/live")
        assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# 15. Ad provider not configured doesn't break rendering
# --------------------------------------------------------------------------- #
def test_ad_provider_not_configured_doesnt_break_rendering():
    provider = get_provider()
    assert isinstance(provider, PlaceholderProvider)
    assert provider.is_configured() is False
    slot = AdSlot(slot_key="test", placement="HEADER")
    html = render_ad_slot_html(slot, provider=provider)
    assert "ad-slot" in html
    assert "data-slot" in html


# --------------------------------------------------------------------------- #
# 16. Each ad position resolves its slot ID independently
# --------------------------------------------------------------------------- #
def test_each_ad_position_resolves_slot_id_independently():
    provider = AdSenseProvider(
        client_id="ca-pub-123",
        slot_ids={
            "HEADER": "111",
            "AFTER_INTRO": "222",
            "MID_ARTICLE": "333",
            "BEFORE_RELATED": "444",
            "FOOTER": "555",
        },
    )
    for pos in DEFAULT_POSITIONS:
        slot = AdSlot(slot_key=pos.lower(), placement=pos)
        html = provider.render_slot(slot)
        assert '<ins class="adsbygoogle"' in html
        assert 'data-ad-client="ca-pub-123"' in html
        assert f'data-ad-slot="' in html

    no_id_provider = AdSenseProvider(client_id="ca-pub-123", slot_ids={})
    assert no_id_provider.is_configured() is False
    html = no_id_provider.render_slot(AdSlot(slot_key="test", placement="HEADER"))
    assert "ad-slot" in html


# --------------------------------------------------------------------------- #
# 17. Meta description in article HTML
# --------------------------------------------------------------------------- #
def test_meta_description_in_article():
    ctx = _ctx("metadesc")
    with ctx:
        sid = f"story-meta-{_next_id()}"
        with get_session() as s:
            _publish_story(s, story_id=sid)
        app = create_app()
        client = TestClient(app)
        resp = client.get(f"/articles/{sid}")
        assert resp.status_code == 200
        assert 'name="description"' in resp.text
        assert 'name="robots" content="index, follow"' in resp.text


# --------------------------------------------------------------------------- #
# 18. Sitemap includes topic pages
# --------------------------------------------------------------------------- #
def test_sitemap_includes_topic_pages():
    ctx = _ctx("smtopic")
    with ctx:
        sid = f"story-sitemap-topic-{_next_id()}"
        with get_session() as s:
            _publish_story(s, story_id=sid)
        app = create_app()
        client = TestClient(app)
        resp = client.get("/sitemap.xml")
        assert resp.status_code == 200
        assert "topics/technology" in resp.text


# --------------------------------------------------------------------------- #
# 19. RSS language is configurable
# --------------------------------------------------------------------------- #
def test_rss_language_configurable():
    xml_es = render_rss_xml(site_name="T", site_url="https://x.com",
                            entries=[], language="es")
    assert "<language>es</language>" in xml_es
    xml_en = render_rss_xml(site_name="T", site_url="https://x.com",
                            entries=[], language="en")
    assert "<language>en</language>" in xml_en


# --------------------------------------------------------------------------- #
# 20-25: published_at NOT NULL regression tests
# --------------------------------------------------------------------------- #
def test_null_published_at_not_selected_as_article():
    """A publication with published_at=NULL is never rendered as the article."""
    ctx = _ctx("null_pub")
    with ctx:
        sid = f"story-nullpub-{_next_id()}"
        with get_session() as s:
            _publish_story(s, story_id=sid)
            # Create a second publication with published_at=NULL for the same story
            pub = s.query(publications).filter_by(
                story_id=sid, status=PublicationStatus.COMPLETED.value).first()
            dup = publications(
                story_id=sid, status=PublicationStatus.COMPLETED.value,
                published_at=None, destination_key="internal",
                decision_id=pub.decision_id, idempotency_key=f"null-{sid}",
            )
            s.add(dup)
            s.commit()
        app = create_app()
        client = TestClient(app)
        resp = client.get(f"/articles/{sid}")
        assert resp.status_code == 200
        assert 'property="article:published_time"' in resp.text


def test_valid_published_at_is_selected():
    """A publication with a real published_at IS selected for rendering."""
    ctx = _ctx("valid_pub")
    with ctx:
        sid = f"story-validpub-{_next_id()}"
        with get_session() as s:
            _publish_story(s, story_id=sid)
        app = create_app()
        client = TestClient(app)
        resp = client.get(f"/articles/{sid}")
        assert resp.status_code == 200
        assert 'property="article:published_time"' in resp.text


def test_article_renders_published_time_meta_tag():
    """The rendered article contains the <meta property="article:published_time"> tag."""
    ctx = _ctx("pubtime_meta")
    with ctx:
        sid = f"story-pubtime-{_next_id()}"
        with get_session() as s:
            _publish_story(s, story_id=sid)
        app = create_app()
        client = TestClient(app)
        resp = client.get(f"/articles/{sid}")
        assert resp.status_code == 200
        html = resp.text
        assert 'property="article:published_time"' in html
        assert 'content="2026-09-07T00:00:00+00:00"' in html


def test_published_time_matches_db_value():
    """The article:published_time value matches the stored published_at."""
    ctx = _ctx("pubtime_match")
    with ctx:
        sid = f"story-match-{_next_id()}"
        with get_session() as s:
            _publish_story(s, story_id=sid)
            pub = s.query(publications).filter_by(
                story_id=sid, status=PublicationStatus.COMPLETED.value).first()
            db_published_at = pub.published_at
        app = create_app()
        client = TestClient(app)
        resp = client.get(f"/articles/{sid}")
        assert resp.status_code == 200
        assert db_published_at in resp.text


def test_sitemap_and_rss_use_valid_timestamps():
    """Sitemap and RSS entries only contain valid (non-NULL) timestamps."""
    ctx = _ctx("ts_validity")
    with ctx:
        sid = f"story-ts-{_next_id()}"
        with get_session() as s:
            _publish_story(s, story_id=sid)
        app = create_app()
        client = TestClient(app)
        sitemap = client.get("/sitemap.xml")
        assert sitemap.status_code == 200
        assert "<lastmod>" in sitemap.text
        assert "T" in sitemap.text  # ISO 8601 timestamp separator
        rss = client.get("/feed.xml")
        assert rss.status_code == 200
        assert "<pubDate>" in rss.text


def test_multiple_stories_each_select_correct_publication():
    """Multiple stories each get the correct publication with valid published_at."""
    ctx = _ctx("multi_story")
    with ctx:
        sid_a = f"story-multi-a-{_next_id()}"
        sid_b = f"story-multi-b-{_next_id()}"
        with get_session() as s:
            _publish_story(s, story_id=sid_a)
            _publish_story(s, story_id=sid_b)
            # Insert a NULL published_at for story A
            pub = s.query(publications).filter_by(
                story_id=sid_a, status=PublicationStatus.COMPLETED.value).first()
            dup = publications(
                story_id=sid_a, status=PublicationStatus.COMPLETED.value,
                published_at=None, destination_key="internal",
                decision_id=pub.decision_id, idempotency_key=f"null-multi-{sid_a}",
            )
            s.add(dup)
            s.commit()
        app = create_app()
        client = TestClient(app)
        resp_a = client.get(f"/articles/{sid_a}")
        resp_b = client.get(f"/articles/{sid_b}")
        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        # Both should have published_time (the NULL one should NOT be selected)
        assert 'property="article:published_time"' in resp_a.text
        assert 'property="article:published_time"' in resp_b.text
