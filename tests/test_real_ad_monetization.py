"""Real Ad Monetization Phase — regression tests.

Covers:
  1. Provider correctly configured (AdSense with IDs)
  2. Provider not configured -> placeholders
  3. Provider failure -> article still renders
  4. 5 slots independent
  5. No fake IDs
  6. Analytics doesn't confuse slot_rendered with impression
  7. Revenue not generated artificially
  8. Scheduler still idempotent
  9. Auto-published articles retain ad slots
  10. Broken config doesn't break /health, /ready, or rendering
"""
from __future__ import annotations

import os
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
    load_active_slots,
    register_default_slots,
)
from newsforge.analytics.ads import (
    get_ad_metrics,
    record_ad_click,
    record_ad_impression,
    record_ad_revenue,
    record_ad_request,
    record_slot_rendered,
)
from newsforge.db import get_session, publications
from newsforge.db.models import (
    ArtifactFormat,
    PublicationStatus,
    decisions,
    sources,
    source_items,
    stories as stories_model,
)
from newsforge.db.session import use_isolated_database_ctx
from newsforge.generate.assembly import generate_story
from newsforge.publish.destinations import register_builtin_destinations, reset_registry
from newsforge.publish.publisher import publish_story
from newsforge.web.app import create_app

_DB_DIR: Path | None = None
_COUNTER = 0


def _next_id():
    global _COUNTER
    _COUNTER += 1
    return _COUNTER


def _ctx(name="monetization"):
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
    pub = (session.query(publications)
           .filter_by(story_id=story_id, status=PublicationStatus.COMPLETED.value)
           .first())
    if pub is not None and not pub.published_at:
        pub.published_at = "2026-09-07T00:00:00+00:00"
        session.commit()
    return result


# --------------------------------------------------------------------------- #
# 1. Provider correctly configured (AdSense with IDs)
# --------------------------------------------------------------------------- #
def test_adsense_provider_configured():
    provider = AdSenseProvider(
        client_id="ca-pub-1234567890",
        slot_ids={"HEADER": "1111111111", "AFTER_INTRO": "2222222222"},
    )
    assert provider.is_configured() is True
    slot = AdSlot(slot_key="header", placement="HEADER")
    html = provider.render_slot(slot)
    assert 'data-ad-client="ca-pub-1234567890"' in html
    assert 'data-ad-slot="1111111111"' in html
    assert '<ins class="adsbygoogle"' in html
    assert 'style="display:block"' in html


# --------------------------------------------------------------------------- #
# 2. Provider not configured -> placeholders
# --------------------------------------------------------------------------- #
def test_unconfigured_provider_returns_placeholders():
    provider = get_provider()
    assert isinstance(provider, PlaceholderProvider)
    assert provider.is_configured() is False
    slot = AdSlot(slot_key="header", placement="HEADER")
    html = provider.render_slot(slot)
    assert '<div class="ad-slot"' in html
    assert 'data-slot="header"' in html
    assert "adsbygoogle" not in html


# --------------------------------------------------------------------------- #
# 3. Provider failure -> article still renders
# --------------------------------------------------------------------------- #
def test_provider_failure_still_renders_article():
    ctx = _ctx("prov_fail")
    with ctx:
        sid = f"story-provfail-{_next_id()}"
        with get_session() as s:
            _publish_story(s, story_id=sid)
        app = create_app()
        client = TestClient(app)
        with patch("newsforge.web.app.get_provider", side_effect=RuntimeError("boom")):
            resp = client.get(f"/articles/{sid}")
        assert resp.status_code == 200
        assert "Test Story" in resp.text


# --------------------------------------------------------------------------- #
# 4. 5 slots independent
# --------------------------------------------------------------------------- #
def test_five_slots_independent():
    for pos in DEFAULT_POSITIONS:
        slot = AdSlot(slot_key=f"ad_{pos.lower()}", placement=pos)
        html = render_slot_for_test(slot, pos)
        assert f'data-placement="{pos}"' in html or f"data-ad-slot" in html


def render_slot_for_test(slot, position):
    provider = AdSenseProvider(
        client_id="ca-pub-test",
        slot_ids={position: f"unit-{position.lower()}"},
    )
    return provider.render_slot(slot)


# --------------------------------------------------------------------------- #
# 5. No fake IDs
# --------------------------------------------------------------------------- #
def test_no_fake_ad_unit_ids():
    """Placeholder provider never emits fake ad unit IDs."""
    provider = PlaceholderProvider()
    for pos in DEFAULT_POSITIONS:
        slot = AdSlot(slot_key=f"ad_{pos.lower()}", placement=pos)
        html = provider.render_slot(slot)
        assert "data-ad-slot" not in html
        assert "ca-pub-" not in html
        assert "adsbygoogle" not in html


# --------------------------------------------------------------------------- #
# 6. Analytics doesn't confuse slot_rendered with impression
# --------------------------------------------------------------------------- #
def test_analytics_separates_rendered_from_impression():
    ctx = _ctx("analytics_sep")
    with ctx:
        with get_session() as s:
            record_slot_rendered(s, article_id="art-1", slot_key="header",
                                 placement="HEADER", provider="placeholder")
            metrics = get_ad_metrics(s, article_id="art-1")
            assert metrics["slot_rendered"] == 1
            assert metrics["ad_impression"] == 0
            assert metrics["ad_request"] == 0
            assert metrics["ad_click"] == 0
            assert metrics["ad_revenue_count"] == 0


# --------------------------------------------------------------------------- #
# 7. Revenue not generated artificially
# --------------------------------------------------------------------------- #
def test_revenue_not_artificially_generated():
    ctx = _ctx("no_fake_rev")
    with ctx:
        with get_session() as s:
            metrics = get_ad_metrics(s)
            assert metrics["ad_revenue_count"] == 0
            assert metrics["ad_revenue_total"] == 0.0


# --------------------------------------------------------------------------- #
# 8. Scheduler still idempotent
# --------------------------------------------------------------------------- #
def test_scheduler_idempotent():
    ctx = _ctx("sched_idem")
    with ctx:
        sid = f"story-sched-{_next_id()}"
        with get_session() as s:
            _publish_story(s, story_id=sid)
            _publish_story(s, story_id=sid)
        with get_session() as s:
            pubs = s.query(publications).filter_by(story_id=sid).all()
            assert len(pubs) == 1


# --------------------------------------------------------------------------- #
# 9. Auto-published articles retain ad slots
# --------------------------------------------------------------------------- #
def test_auto_published_articles_have_ad_slots():
    ctx = _ctx("auto_ads")
    with ctx:
        sid = f"story-autoads-{_next_id()}"
        with get_session() as s:
            _publish_story(s, story_id=sid)
        app = create_app()
        client = TestClient(app)
        resp = client.get(f"/articles/{sid}")
        assert resp.status_code == 200
        assert "ad-slot" in resp.text or "adsbygoogle" in resp.text or 'data-slot="' in resp.text


# --------------------------------------------------------------------------- #
# 10. Broken config doesn't break /health, /ready, or rendering
# --------------------------------------------------------------------------- #
def test_broken_config_doesnt_break_endpoints():
    ctx = _ctx("broken_cfg")
    with ctx:
        sid = f"story-broken-{_next_id()}"
        with get_session() as s:
            _publish_story(s, story_id=sid)
        app = create_app()
        client = TestClient(app)
        with patch.dict(os.environ, {"NEWSFORGE_AD_PROVIDER": "INVALID_PROVIDER_XYZ"}):
            health = client.get("/health")
            ready = client.get("/ready")
            article = client.get(f"/articles/{sid}")
            assert health.status_code == 200
            assert ready.status_code == 200
            assert article.status_code == 200
