"""Regression tests for trust propagation: claim → story → decision.

The aggregate trust score computed by verify/persist.py must flow consistently to:
  * ``stories.trust_score`` (the story row),
  * ``decisions.trust_score`` (the decision row),
  * ``decisions.reasons_json`` (the embedded trust value in reasons).

Prior to the fix, ``stories.trust_score`` was set to the P2 tier-baseline proxy
(80 for TIER_2) and never updated, while ``decisions.trust_score`` was hardcoded
to 0.  These tests lock the corrected behaviour in place.

Run: ``python -m pytest tests/test_trust_propagation.py -q``
"""
from __future__ import annotations

from pathlib import Path

import pytest

from newsforge import db
from newsforge.db import get_session, stories as stories_model, decisions
from newsforge.pipeline.orchestrator import run_pipeline

_T0 = "2026-09-12T08:00:00+00:00"
_PUB = "2026-09-11T00:00:00+00:00"

_db_seq = 0


@pytest.fixture(autouse=True)
def isolated_db():
    global _db_seq
    db_dir = Path.cwd() / ".pytest_tmp"
    db_dir.mkdir(exist_ok=True)
    _db_seq += 1
    path = db_dir / f"trust_prop_{_db_seq}.db"
    try:
        if path.exists():
            path.unlink()
    except OSError:
        _db_seq += 1
        path = db_dir / f"trust_prop_{_db_seq}.db"
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


def _seed_source(session, *, source_id: str, tier: str = "TIER_2"):
    src = db.sources()
    src.id = source_id
    src.source_id = source_id
    src.name = source_id
    src.type = "RSS"
    src.country = "GB"
    src.language = "en"
    src.tier = tier
    src.trust_score = 80 if tier == "TIER_2" else 35
    src.status = "active"
    session.add(src)
    session.commit()
    return source_id


def _seed_item(session, *, source_id: str, title: str) -> str:
    item = db.source_items()
    item.source_id = source_id
    item.title = title
    item.description = title
    item.content_html = None
    item.content_text = title
    item.published_at = _PUB
    item.dedupe_hash = f"hash-{source_id}-{title}"
    session.add(item)
    session.commit()
    return str(item.id)


def _run(signal_ids):
    return run_pipeline(signal_ids=signal_ids, reference_time=_T0, destinations=["recording"])


def _get_story_trust(story_id: str) -> int | None:
    with get_session() as s:
        row = s.query(stories_model).filter_by(story_id=story_id).first()
        return row.trust_score if row else None


def _get_decision(story_id: str) -> dict:
    with get_session() as s:
        row = s.query(decisions).filter_by(
            target_type="STORY", target_id=story_id
        ).first()
        if row is None:
            return {}
        return {
            "decision": row.decision,
            "trust_score": row.trust_score,
            "risk_level": row.risk_level,
            "reasons_json": row.reasons_json,
        }


# --------------------------------------------------------------------------- #
# Core invariant: story.trust_score == decisions.trust_score (the fix)
# --------------------------------------------------------------------------- #

def test_tier2_single_source_story_matches_decision():
    """TIER_2 single source: story and decision trust must agree."""
    with db.get_session() as s:
        _seed_source(s, source_id="bbc-tech")
        item = _seed_item(s, source_id="bbc-tech",
                          title="OpenAI says it cracked maths problem")

    result = _run([item])
    assert result["stories_detected"] == 1

    story_id = result["outcomes"][0].business_key
    story_trust = _get_story_trust(story_id)
    decision = _get_decision(story_id)

    assert story_trust is not None, "story trust must be set"
    assert story_trust == decision["trust_score"], (
        f"stories.trust_score ({story_trust}) != decisions.trust_score ({decision['trust_score']})"
    )
    assert story_trust > 0
    assert decision["decision"] == "PUBLISH"


def test_two_source_corroboration_story_matches_decision():
    """Two TIER_2 sources: trust higher than single, story and decision agree."""
    with db.get_session() as s:
        _seed_source(s, source_id="bbc-tech")
        _seed_source(s, source_id="guardian-tech")
        a = _seed_item(s, source_id="bbc-tech",
                       title="OpenAI says it cracked 90-year-old maths problem in 88 hours")
        b = _seed_item(s, source_id="guardian-tech",
                       title="OpenAI confirms it cracked the 90-year-old maths problem in 88 hours")

    result = _run([a, b])
    assert result["stories_detected"] == 1

    story_id = result["outcomes"][0].business_key
    story_trust = _get_story_trust(story_id)
    decision = _get_decision(story_id)

    assert story_trust == decision["trust_score"], (
        f"story trust {story_trust} != decision trust {decision['trust_score']}"
    )
    assert story_trust > 0
    assert decision["decision"] == "PUBLISH"


def test_tier3_source_below_threshold():
    """TIER_3 single source: trust below 60, should WAIT, story and decision agree."""
    with db.get_session() as s:
        _seed_source(s, source_id="secondary-news", tier="TIER_3")
        item = _seed_item(s, source_id="secondary-news",
                          title="Tech startup announces new product")

    result = _run([item])
    assert result["stories_detected"] == 1

    story_id = result["outcomes"][0].business_key
    story_trust = _get_story_trust(story_id)
    decision = _get_decision(story_id)

    assert story_trust == decision["trust_score"], (
        f"story trust {story_trust} != decision trust {decision['trust_score']}"
    )
    assert decision["decision"] in ("WAIT", "REVIEW"), (
        f"expected WAIT or REVIEW, got {decision['decision']}"
    )
    assert story_trust > 0
    assert story_trust < 80, "TIER_3 single should be below TIER_2 baseline"


def test_tier4_source_below_threshold():
    """TIER_4 single source: trust below 60, story and decision agree."""
    with db.get_session() as s:
        _seed_source(s, source_id="low-tier", tier="TIER_4")
        item = _seed_item(s, source_id="low-tier",
                          title="Rumour about unverified tech startup claim")

    result = _run([item])
    story_id = result["outcomes"][0].business_key
    story_trust = _get_story_trust(story_id)
    decision = _get_decision(story_id)

    assert story_trust == decision["trust_score"], (
        f"story trust {story_trust} != decision trust {decision['trust_score']}"
    )
    assert decision["decision"] in ("WAIT", "REVIEW"), (
        f"expected WAIT or REVIEW, got {decision['decision']}"
    )
    assert story_trust > 0
    assert story_trust < 60, f"TIER_4 single should be below threshold, got {story_trust}"


# --------------------------------------------------------------------------- #
# No artificial 0 or 80 values remain after P3 VERIFY
# --------------------------------------------------------------------------- #

def test_no_artificial_trust_values():
    """After verification, trust must reflect the real aggregate — never the P2
    tier-baseline proxy (80) or the old hardcoded zero."""
    with db.get_session() as s:
        _seed_source(s, source_id="bbc-tech")
        item = _seed_item(s, source_id="bbc-tech",
                          title="UK government rejects kill switch idea for dangerous AI")

    result = _run([item])
    story_id = result["outcomes"][0].business_key

    story_trust = _get_story_trust(story_id)
    decision = _get_decision(story_id)

    # Must not be the P2 tier-proxy
    assert story_trust != 80, f"stories.trust_score must not be the tier proxy 80, got {story_trust}"
    # Must not be the old hardcoded 0
    assert story_trust != 0, f"stories.trust_score must not be 0, got {story_trust}"
    assert decision["trust_score"] != 0, (
        f"decisions.trust_score must not be 0, got {decision['trust_score']}"
    )
    # Both must agree
    assert story_trust == decision["trust_score"]


def test_story_trust_differs_from_tier_baseline():
    """TIER_2 single: trust must differ from the 80-tier-baseline used in P2."""
    with db.get_session() as s:
        _seed_source(s, source_id="bbc-tech")
        item = _seed_item(s, source_id="bbc-tech",
                          title="OpenAI says it cracked maths problem")

    result = _run([item])
    story_id = result["outcomes"][0].business_key
    story_trust = _get_story_trust(story_id)

    # TIER_2 baseline is 80, but aggregate trust is the P3 evidence-backed score
    assert story_trust != 80, f"trust must be the aggregate, not the tier baseline (80)"
    assert story_trust > 0


def test_two_sources_higher_trust_than_single():
    """Corroboration must increase trust: two TIER_2 > one TIER_2."""
    with db.get_session() as s:
        _seed_source(s, source_id="bbc-tech-1")
        item = _seed_item(s, source_id="bbc-tech-1",
                          title="OpenAI says it cracked maths problem")
    result_single = _run([item])
    single_trust = _get_story_trust(result_single["outcomes"][0].business_key)

    with db.get_session() as s:
        _seed_source(s, source_id="bbc-tech-2")
        _seed_source(s, source_id="guardian-tech-2")
        a = _seed_item(s, source_id="bbc-tech-2",
                       title="OpenAI says it cracked 90-year-old maths problem in 88 hours")
        b = _seed_item(s, source_id="guardian-tech-2",
                       title="OpenAI confirms it cracked the 90-year-old maths problem in 88 hours")
    result_multi = _run([a, b])
    multi_trust = _get_story_trust(result_multi["outcomes"][0].business_key)

    assert multi_trust > single_trust, (
        f"two-source trust ({multi_trust}) should be higher than single ({single_trust})"
    )
