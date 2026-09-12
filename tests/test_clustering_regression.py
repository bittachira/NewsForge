"""Regression tests for event-signature story clustering (the contamination fix).

The old detector grouped items by ``(topic, year)`` plus one cluster-wide dominant
entity phrase, which picked feed boilerplate ("Continue", "Co") and fused every
technology item into a single ``continue_technology_2026`` story. A single RED
claim (Anthropic bioweapons) inside that bucket then blocked unrelated events
from ever publishing.

The new detector keys every item on its own *event signature*
``subject [+ product] + (concepts-or-kind) + year``. These tests lock that
behaviour in place using VERBATIM real feed headlines (bbc_tech/guardian_tech,
2026-09-10/11):

- genuinely same-event pairs fuse into ONE story with ONE canonical STORY_ID,
- different events keep DIFFERENT stories even when they share an entity,
- story keys are deterministic and independent of input order,
- end-to-end: the RED bioweapons story WAITs while a separate corroborated
  OpenAI-maths story PUBLISHes in the same pipeline run (no contamination).

Run: ``python -m pytest tests/test_clustering_regression.py -q``
"""
from __future__ import annotations

from pathlib import Path

import pytest

from newsforge import db
from newsforge.pipeline.orchestrator import run_pipeline
from newsforge.stories.detector import cluster_items

_T0 = "2026-09-12T08:00:00+00:00"
_PUB = "2026-09-11T00:00:00+00:00"

_db_seq = 0


@pytest.fixture(autouse=True)
def isolated_db():
    global _db_seq
    db_dir = Path.cwd() / ".pytest_tmp"
    db_dir.mkdir(exist_ok=True)
    _db_seq += 1
    path = db_dir / f"cluster_{_db_seq}.db"
    try:
        if path.exists():
            path.unlink()
    except OSError:
        _db_seq += 1
        path = db_dir / f"cluster_{_db_seq}.db"
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


def _seed_item(session, *, source_id: str, title: str, description: str | None = None) -> str:
    item = db.source_items()
    item.source_id = source_id
    item.title = title
    item.description = description
    item.content_html = None
    item.content_text = description or title
    item.published_at = _PUB
    item.dedupe_hash = f"hash-{source_id}-{title}"
    session.add(item)
    session.commit()
    return str(item.id)


def _run(signal_ids):
    return run_pipeline(signal_ids=signal_ids, reference_time=_T0, destinations=["recording"])


# --------------------------------------------------------------------------- #
# Verbatim real feed headlines (bbc_tech / guardian_tech, 2026-09-10/11)
# --------------------------------------------------------------------------- #
_BBC_BIO = "Anthropic blocks possible attempt to use AI to make biological weapons"
_GDN_BIO = "Anthropic details bad actors' efforts to misuse its AI for bioweapons"
_BBC_PCT = "Anthropic researcher believes more than 10% chance AI 'could kill all humans'"
_GDN_MORE = "More Anthropic researchers warn of AI's perils but Musk dismisses 'psyop'"
_BBC_META = "Meta continues to run ads promoting child sexual abuse material in India - report"
_GDN_BOSS = "Instagram boss says users will be 'overwhelmed' with brand content in algorithm-free world"
_BBC_SCAM = "Scammers demand ransoms from Instagram users over fake copyright claims"
_GDN_PRO = "Pixel 11 Pro review: Google's best pocket camera goes customisable"
_GDN_PIXEL = "Pixel 11 review: Google sets the bar for standard flagship phones"
_BBC_OPENAI = "OpenAI says it cracked 90-year-old maths problem in 88 hours"
_BBC_GOOGLE = "Google picks Finland for its largest single investment in Europe"
_BBC_UK = "UK government rejects kill switch idea for dangerous AI"

_ALL_HEADLINES = [
    _BBC_BIO,
    _GDN_BIO,
    _BBC_PCT,
    _GDN_MORE,
    _BBC_META,
    _GDN_BOSS,
    _BBC_SCAM,
    _GDN_PRO,
    _GDN_PIXEL,
    _BBC_OPENAI,
    _BBC_GOOGLE,
    _BBC_UK,
]


def _item(title: str) -> dict:
    return {"title": title, "published_at": _PUB}


def _keys_for(titles: list[str]) -> dict[str, str]:
    clusters = cluster_items([_item(t) for t in titles])
    key_of = {}
    for key, group in clusters.items():
        for item in group:
            key_of[item["title"]] = key
    return key_of


# --------------------------------------------------------------------------- #
# Same-event pairs fuse to one canonical STORY_ID
# --------------------------------------------------------------------------- #
def test_bioweapons_pair_fuses_into_one_canonical_story():
    key = _keys_for([_BBC_BIO, _GDN_BIO])
    assert key[_BBC_BIO] == "anthropic_biological_weapons_2026"
    assert key[_GDN_BIO] == "anthropic_biological_weapons_2026"
    assert len(cluster_items([_item(_BBC_BIO), _item(_GDN_BIO)])) == 1


def test_existential_risk_pair_fuses_into_one_canonical_story():
    key = _keys_for([_BBC_PCT, _GDN_MORE])
    assert key[_BBC_PCT] == "anthropic_existential_risk_2026"
    assert key[_GDN_MORE] == "anthropic_existential_risk_2026"
    assert len(cluster_items([_item(_BBC_PCT), _item(_GDN_MORE)])) == 1


def test_same_entity_same_safety_context_is_split_into_different_events():
    """Two Anthropic event frames (bioweapons vs existential risk) NEVER fuse."""
    key = _keys_for([_BBC_BIO, _GDN_BIO, _BBC_PCT, _GDN_MORE])
    assert key[_BBC_BIO] == "anthropic_biological_weapons_2026"
    assert key[_BBC_PCT] == "anthropic_existential_risk_2026"
    assert key[_BBC_BIO] != key[_BBC_PCT]


# --------------------------------------------------------------------------- #
# Different events with shared entities/signals STAY in different stories
# --------------------------------------------------------------------------- #
def test_instagram_events_never_merge_into_one_instagram_story():
    """Meta-CSA report, Mosseri announcement and the ransom scam are 3 stories."""
    key = _keys_for([_BBC_META, _GDN_BOSS, _BBC_SCAM])
    assert key[_BBC_META] == "meta_child_sexual_abuse_2026"
    assert key[_GDN_BOSS] == "instagram_announce_2026"
    assert key[_BBC_SCAM] == "instagram_copyright_scam_2026"
    assert len({key[_BBC_META], key[_GDN_BOSS], key[_BBC_SCAM]}) == 3


def test_pixel_11_and_pixel_11_pro_reviews_stay_separate():
    """Two product reviews of different phones: product slot disambiguates."""
    key = _keys_for([_GDN_PRO, _GDN_PIXEL])
    assert key[_GDN_PRO] == "google_pixel_11_pro_product_review_2026"
    assert key[_GDN_PIXEL] == "google_pixel_11_product_review_2026"
    assert len(cluster_items([_item(_GDN_PRO), _item(_GDN_PIXEL)])) == 2


def test_google_news_events_stay_separate():
    """Google investment and UK kill-switch AI stories are different events."""
    key = _keys_for([_BBC_GOOGLE, _BBC_UK])
    assert key[_BBC_GOOGLE] == "google_investment_2026"
    assert key[_BBC_UK] == "uk_ai_safety_2026"
    assert key[_BBC_GOOGLE] != key[_BBC_UK]


def test_tech_block_splits_into_coherent_stories_no_continue_technology_bucket():
    """The old continue_technology_2026 super-bucket is gone."""
    key = _keys_for(_ALL_HEADLINES)
    keys = set(key.values())
    assert not any("continue_technology" in k for k in keys)
    assert not any(k == "co_2026" for k in keys)
    # Each real pair/triple keeps its dedicated story key.
    assert key[_BBC_BIO] == "anthropic_biological_weapons_2026"
    assert key[_BBC_OPENAI] == "openai_mathematics_2026"
    assert key[_BBC_GOOGLE] == "google_investment_2026"


# --------------------------------------------------------------------------- #
# Determinism / stability
# --------------------------------------------------------------------------- #
def test_story_keys_are_independent_of_input_order():
    forward = _keys_for(_ALL_HEADLINES)
    reverse = _keys_for(list(reversed(_ALL_HEADLINES)))
    assert forward == reverse


def test_story_ids_are_stable_across_repeated_clustering():
    first = cluster_items([_item(t) for t in _ALL_HEADLINES])
    second = cluster_items([_item(t) for t in _ALL_HEADLINES])
    assert set(first) == set(second)
    for key in first:
        assert {i["title"] for i in first[key]} == {i["title"] for i in second[key]}


# --------------------------------------------------------------------------- #
# End-to-end contamination regression: a RED claim must not block unrelated events.
# The pair below runs in ONE pipeline run; the Anthropic story carries RED risk
# ("weapons") and must WAIT, while a corroborated OpenAI maths story (BBC + a
# pattern-consistent second outlet) must still PUBLISH.
# --------------------------------------------------------------------------- #
_GDN_OPENAI = "OpenAI confirms it cracked the 90-year-old maths problem in 88 hours"


def test_red_bioweapons_story_does_not_contaminate_separate_maths_story():
    with db.get_session() as s:
        _seed_source(s, source_id="bbc-tech")
        _seed_source(s, source_id="guardian-tech")
        bio_bbc = _seed_item(s, source_id="bbc-tech", title=_BBC_BIO)
        bio_gdn = _seed_item(s, source_id="guardian-tech", title=_GDN_BIO)
        maths_bbc = _seed_item(s, source_id="bbc-tech", title=_BBC_OPENAI)
        maths_gdn = _seed_item(s, source_id="guardian-tech", title=_GDN_OPENAI)

    result = _run([bio_bbc, bio_gdn, maths_bbc, maths_gdn])

    assert result["stories_detected"] == 2
    out_by_key = {o.business_key: o for o in result["outcomes"]}
    assert set(out_by_key) == {"anthropic_biological_weapons_2026", "openai_mathematics_2026"}

    # The Anthropic story is RED ("weapons") -> the hard rule holds: never publish.
    bioweapons = out_by_key["anthropic_biological_weapons_2026"]
    assert bioweapons.decision["risk_level"] == "RED"
    assert bioweapons.final_status == "WAIT"

    # The unrelated maths event is corroborated by two independent outlets and,
    # because it lives in its OWN story, publishes without being blocked by it.
    maths = out_by_key["openai_mathematics_2026"]
    assert maths.final_status == "PUBLISHED", f"{maths.error}"
    assert maths.decision["decision"] == "PUBLISH"
    assert maths.decision["trust_score"] >= 60
    for te in maths.decision["trust_evaluations"]:
        assert te["independent_corroboration"] == 2

    # The maths claims never gesture at the bioweapons evidence (or vice versa).
    with db.get_session() as s:
        for bk, expect_ev in (
            ("anthropic_biological_weapons_2026", 2),
            ("openai_mathematics_2026", 2),
        ):
            rows = s.query(db.claims).filter_by(story_id=bk).all()
            assert len(rows) == 2, bk
            for claim in rows:
                ev = s.query(db.claim_evidence).filter_by(claim_id=str(claim.id)).all()
                assert len(ev) == expect_ev, (bk, claim.claim_id)