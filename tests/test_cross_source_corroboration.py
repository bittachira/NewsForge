"""Regression tests: real cross-source corroboration must unlock ACCEPT verdicts.

Root cause this suite guards against (RUN 6a4c1d57): the default claim-spec
builder attached ONLY the claim's own source_item as evidence, so every claim
looked "single-source" to the Trust Engine even when two genuinely independent
sources were clustered into the SAME story. Corroboration never exceeded 1 and
trust could never reach the publish bar on a TIER_3 story with any risk/freshness
deduction.

The tests below drive the REAL pipeline (`run_pipeline` + default builder) so the
fix is proven end-to-end: detection -> clustering -> claims/evidence ->
corroboration -> trust -> decision.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from newsforge import db
from newsforge.db import decisions, get_session, publications
from newsforge.pipeline.orchestrator import (
    _default_claim_spec_builder,
    run_pipeline,
)

# One shared reference clock; every seeded item is ~8h old => fresh (100 pts).
_T0 = "2026-09-12T08:00:00+00:00"
_PUB = "2026-09-12T00:00:00+00:00"

_db_seq = 0


@pytest.fixture(autouse=True)
def isolated_db():
    global _db_seq
    db_dir = Path.cwd() / ".pytest_tmp"
    db_dir.mkdir(exist_ok=True)
    _db_seq += 1
    path = db_dir / f"xcorr_{_db_seq}.db"
    try:
        if path.exists():
            path.unlink()
    except OSError:
        _db_seq += 1
        path = db_dir / f"xcorr_{_db_seq}.db"
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


def _seed_source(session, *, id: str, tier: str = "TIER_3"):
    src = db.sources()
    src.id = id
    src.source_id = id
    src.name = id
    src.type = "RSS"
    src.country = "GB"
    src.language = "en"
    src.tier = tier
    src.trust_score = 60 if tier == "TIER_3" else 35
    src.status = "active"
    session.add(src)
    session.commit()
    return id


def _seed_item(session, *, source_id: str, title: str) -> str:
    item = db.source_items()
    item.source_id = source_id
    item.title = title
    item.description = None
    item.content_html = None
    item.content_text = title
    item.published_at = _PUB
    item.dedupe_hash = f"hash-{source_id}-{title}"
    session.add(item)
    session.commit()
    return str(item.id)


# Both headlines match the economy topic ("interest rate"/"market" keywords),
# share the entity "Central Bank" and carry the low-severity word "market"
# (YELLOW => -3 trust deduction). That makes a SINGLE-source story fall just
# below the 60 trust bar while a genuinely corroborated one passes.
_HEADLINES = [
    "Central Bank preserves interest rates after volatile market week",
    "Central Bank keeps interest rates amid volatile markets",
]


def _run(signal_ids):
    # Pin a single destination so publication-row counts are unambiguous.
    return run_pipeline(signal_ids=signal_ids, reference_time=_T0,
                        destinations=["recording"])


# --------------------------------------------------------------------------- #
# Bug reproduction: two articles from the SAME source must NOT count as 1... no,
# as corroboration; they must still collapse to ONE independent source.
# --------------------------------------------------------------------------- #
def test_same_source_items_do_not_create_fake_corroboration():
    with get_session() as s:
        _seed_source(s, id="src-euronews")
        ids = [_seed_item(s, source_id="src-euronews", title=t) for t in _HEADLINES]

    result = _run(ids)

    assert result["stories_detected"] == 1, "both items must cluster into one story"
    outcome = result["outcomes"][0]
    decision = outcome.decision
    te = decision["trust_evaluations"][0]
    assert te["independent_corroboration"] == 1, "same outlet == one independent source"
    assert te["total_evidence"] == 2, "both linked items are counted as evidence"
    assert decision["trust_score"] < 60, f"single-source trust must stay below bar: {decision['trust_score']}"
    assert outcome.final_status == "WAIT", (
        f"single-source story must not publish: {outcome.final_status}"
    )


# --------------------------------------------------------------------------- #
# The fix: two genuinely independent sources on the same story must be counted
# as 2 independent sources and cross the trust bar legitimately.
# --------------------------------------------------------------------------- #
def test_independent_sources_unlock_publish():
    with get_session() as s:
        _seed_source(s, id="src-alpha")
        _seed_source(s, id="src-beta")
        item_a = _seed_item(s, source_id="src-alpha", title=_HEADLINES[0])
        item_b = _seed_item(s, source_id="src-beta", title=_HEADLINES[1])

    result = _run([item_a, item_b])

    assert result["stories_detected"] == 1, "both sources must cluster into one story"
    outcome = result["outcomes"][0]
    decision = outcome.decision
    assert outcome.final_status == "PUBLISHED", (
        f"corroborated story should publish, got {outcome.final_status}: {outcome.error}"
    )
    assert decision["decision"] == "PUBLISH"
    assert decision["trust_score"] >= 60, f"trust too low: {decision['trust_score']}"
    for te in decision["trust_evaluations"]:
        assert te["independent_corroboration"] == 2
        assert te["total_evidence"] == 2

    with get_session() as s:
        assert s.query(publications).count() == 1


# --------------------------------------------------------------------------- #
# Builder unit regression: own item first, sibling items attached as evidence.
# --------------------------------------------------------------------------- #
def test_default_builder_attaches_story_evidence():
    with get_session() as s:
        _seed_source(s, id="src-a")
        _seed_source(s, id="src-b")
        item_a = _seed_item(s, source_id="src-a", title=_HEADLINES[0])
        item_b = _seed_item(s, source_id="src-b", title=_HEADLINES[1])
        st = db.stories()
        st.story_id = "central_bank_economy_2026"
        st.slug = "central_bank_economy_2026"
        st.title = _HEADLINES[0]
        session = s
        session.add(st)
        session.flush()
        for iid in (item_a, item_b):
            sig = db.story_signals()
            sig.story_id = st.story_id
            sig.item_id = iid
            session.add(sig)
        session.commit()

    with get_session() as s:
        specs = _default_claim_spec_builder(s, "handle", "central_bank_economy_2026")

    assert len(specs) == 2
    for spec in specs:
        # Own item is first in the evidence list, both items are attached.
        assert spec["source_item_ids"][0] in (item_a, item_b)
        assert set(spec["source_item_ids"]) == {item_a, item_b}
        assert spec["tiers"] == ["TIER_3"]


# --------------------------------------------------------------------------- #
# Idempotency: the corroborated run is deterministic and creates no duplicates.
# --------------------------------------------------------------------------- #
def test_corroborated_pipeline_is_idempotent():
    with get_session() as s:
        _seed_source(s, id="src-a")
        _seed_source(s, id="src-b")
        item_a = _seed_item(s, source_id="src-a", title=_HEADLINES[0])
        item_b = _seed_item(s, source_id="src-b", title=_HEADLINES[1])

    run1 = _run([item_a, item_b])
    run2 = _run([item_a, item_b])

    assert run1["outcomes"][0].final_status == "PUBLISHED"
    assert run2["outcomes"][0].final_status == "PUBLISHED"
    handle = run1["outcomes"][0].story_handle
    assert handle == run2["outcomes"][0].story_handle

    with get_session() as s:
        assert s.query(publications).filter_by(story_id=run1["outcomes"][0].business_key).count() == 1
        assert s.query(decisions).filter_by(target_id=run1["outcomes"][0].business_key).count() == 1
        # Story handle (UUID pk) is not a decision key column; identity is by business key.
        assert s.query(decisions).count() == 1


# --------------------------------------------------------------------------- #
# Semantic integrity: a cross-source item corroborates ONLY the claims it really
# supports. Case: BBC + Guardian covering the SAME event must produce two
# claim_evidence rows per claim and COUNT as 2 distinct sources.
# --------------------------------------------------------------------------- #
_BBC_SAME_EVENT = (
    "Anthropic blocks possible attempt to use AI to make biological weapons"
)
_GUARDIAN_SAME_EVENT = (
    "Anthropic details efforts to misuse its AI for dangerous biology projects"
)


def test_evidence_matches_pure_predicate():
    from newsforge.verify.claims import evidence_matches

    assert evidence_matches(_BBC_SAME_EVENT, _GUARDIAN_SAME_EVENT)
    assert not evidence_matches(
        "UK government rejects kill switch idea for dangerous AI",
        "Google picks Finland for Europe's largest AI data centre investment",
    )


def test_cross_source_same_event_persists_two_evidence_rows():
    with get_session() as s:
        _seed_source(s, id="bbc-tech", tier="TIER_2")
        _seed_source(s, id="guardian-tech", tier="TIER_2")
        item_bbc = _seed_item(s, source_id="bbc-tech", title=_BBC_SAME_EVENT)
        item_gdn = _seed_item(s, source_id="guardian-tech", title=_GUARDIAN_SAME_EVENT)

    result = _run([item_bbc, item_gdn])

    assert result["stories_detected"] == 1, "both sources must cluster into one story"
    outcome = result["outcomes"][0]
    bk = outcome.business_key

    with get_session() as s:
        claim_rows = s.query(db.claims).filter_by(story_id=bk).all()
        assert len(claim_rows) == 2, "one claim per linked item"
        for claim in claim_rows:
            ev_rows = s.query(db.claim_evidence).filter_by(claim_id=str(claim.id)).all()
            assert len(ev_rows) == 2, "the other source must be persisted as evidence"
            source_ids = {
                str(s.get(db.source_items, str(e.source_item_id)).source_id)
                for e in ev_rows
            }
            assert source_ids == {"bbc-tech", "guardian-tech"}, source_ids

    for te in outcome.decision["trust_evaluations"]:
        assert te["independent_corroboration"] == 2
        assert te["total_evidence"] == 2


# --------------------------------------------------------------------------- #
# No false corroboration: two items in the SAME story but about DIFFERENT events
# must each keep exactly one evidence row and never reach the publish bar.
# --------------------------------------------------------------------------- #
def test_same_story_unrelated_items_stay_single_source():
    with get_session() as s:
        _seed_source(s, id="src-x", tier="TIER_3")
        _seed_source(s, id="src-y", tier="TIER_3")
        item_a = _seed_item(
            s, source_id="src-x",
            title="UK government rejects kill switch idea for dangerous AI",
        )
        item_b = _seed_item(
            s, source_id="src-y",
            title="Google picks Finland for Europe's largest AI data centre investment",
        )

    result = _run([item_a, item_b])

    assert result["stories_detected"] == 1, "both items must share the technology story"
    outcome = result["outcomes"][0]
    bk = outcome.business_key

    with get_session() as s:
        claim_rows = s.query(db.claims).filter_by(story_id=bk).all()
        assert len(claim_rows) == 2
        for claim in claim_rows:
            ev_rows = s.query(db.claim_evidence).filter_by(claim_id=str(claim.id)).all()
            assert len(ev_rows) == 1, "unrelated items must NOT cross-corroborate"

    for te in outcome.decision["trust_evaluations"]:
        assert te["independent_corroboration"] == 1
    assert outcome.final_status != "PUBLISHED"