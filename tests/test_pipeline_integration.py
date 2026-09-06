"""End-to-end integration gate for the P3 verification pipeline (G2, G4, G5, provenance, bypass).

Every test drives the REAL ``run_verification`` against an isolated database and then inspects both
the returned verdict and the persisted rows. It never calls ``decide`` / ``evaluate_quality`` /
``evaluate_trust`` directly — we want to prove the whole pipeline (and its persistence) enforces the
safety invariants, not just the individual functions.

Safety invariants under test (§8):
    RED -> NEVER PUBLISH            unsupported critical claim -> NEVER PUBLISH
    contradiction -> NEVER PUBLISH  insufficient trust -> NEVER PUBLISH
    failed quality gate -> NEVER PUBLISH
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import newsforge.db as db
from newsforge.db.models import from_jsonable

from newsforge.db import (
    get_session, sources, source_items, stories, claim_evidence, claims,
    decisions, audit_logs, trust_evaluations, quality_evaluations, review_tasks,
)
from newsforge.verify.persist import run_verification, build_provenance_chain


@pytest.fixture(autouse=True)
def isolated_db():
    db_dir = Path.cwd() / ".pytest_tmp"
    shutil.rmtree(db_dir, ignore_errors=True)
    db_dir.mkdir(exist_ok=True)
    path = db_dir / "test.db"
    with db.use_isolated_database_ctx(path):
        yield
    shutil.rmtree(db_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Seeding helpers
# --------------------------------------------------------------------------- #
def _seed_source(session, *, id, name, tier="TIER_1"):
    src = sources()
    src.id = id
    src.source_id = id
    src.name = name
    src.type = "RSS"
    src.country = "ES"
    src.language = "es"
    src.tier = tier
    src.trust_score = 90 if tier == "TIER_1" else 35
    src.status = "active"
    session.add(src)
    session.commit()


def _seed_item(session, *, title, source_id, published_at="2026-09-05T10:00:00+00:00"):
    item = source_items()
    item.source_id = source_id
    item.title = title
    item.description = None
    item.content_html = None
    item.content_text = title
    item.published_at = published_at
    item.dedupe_hash = f"hash-{title}"
    session.add(item)
    session.commit()
    return str(item.id)


def _seed_story(session, story_id="my-story-1"):
    st = stories()
    st.story_id = story_id
    st.slug = story_id
    st.title = "My Story"
    session.add(st)
    session.commit()
    return story_id


# --------------------------------------------------------------------------- #
# Test A — RED risk must NEVER auto-publish (Test A)
# --------------------------------------------------------------------------- #
def test_red_risk_never_publishes():
    with get_session() as s:
        _seed_source(s, id="src-red", name="Rumor-Outlet", tier="TIER_4")
        item = _seed_item(s, title="accusation", source_id="src-red")

    result = run_verification(claims_specs=[{
        "claim_id": "c-red",
        "text": "The minister is accused of embezzling public funds.",
        "story_id": "my-story-1",
        "source_item_ids": [item],
        "tiers": ["TIER_4"],
    }], story_id="my-story-1")

    assert result["decision"] != "PUBLISH", f"RED content must never auto-publish: {result['decision']}"
    with get_session() as s:
        row = s.query(decisions).filter_by(target_type="STORY").first()
        assert row is not None and row.decision != "PUBLISH"


# --------------------------------------------------------------------------- #
# Test B — unsupported critical claim must NEVER auto-publish (Test B)
# --------------------------------------------------------------------------- #
def test_unsupported_claim_never_publishes():
    with get_session() as s:
        _seed_source(s, id="src-good", name="Good Outlet", tier="TIER_1")
        item = _seed_item(s, title="good", source_id="src-good")

    result = run_verification(claims_specs=[{
        "claim_id": "c-unsupported",
        "text": "The government approved the measure.",
        "story_id": "my-story-1",
        # deliberately no evidence -> unsupported critical claim
    }], story_id="my-story-1")

    assert result["decision"] != "PUBLISH", f"unsupported claim must never auto-publish: {result['decision']}"
    assert result["quality_passed"] is False
    with get_session() as s:
        row = s.query(decisions).filter_by(target_type="STORY").first()
        assert row is not None and row.decision != "PUBLISH"


# --------------------------------------------------------------------------- #
# Test C — contradiction must NEVER auto-publish (Test C)
# --------------------------------------------------------------------------- #
def test_contradiction_never_publishes():
    with get_session() as s:
        _seed_source(s, id="src-a", name="Source A", tier="TIER_1")
        item_a = _seed_item(s, title="approved", source_id="src-a")
        _seed_source(s, id="src-b", name="Source B", tier="TIER_1")
        item_b = _seed_item(s, title="rejected", source_id="src-b")

    claim_specs = [
        {"claim_id": "c-approve", "text": "The project was approved.",
         "story_id": "my-story-1", "source_item_ids": [item_a], "tiers": ["TIER_1"]},
        {"claim_id": "c-reject", "text": "The project was rejected.",
         "story_id": "my-story-1", "source_item_ids": [item_b], "tiers": ["TIER_1"]},
    ]

    result = run_verification(claims_specs=claim_specs, story_id="my-story-1")
    assert result["decision"] != "PUBLISH", f"contradiction must never auto-publish: {result['decision']}"
    with get_session() as s:
        row = s.query(decisions).filter_by(target_type="STORY").first()
        assert row is not None and row.decision != "PUBLISH"


# --------------------------------------------------------------------------- #
# Test D — a high-trust story must NOT lift a new low-trust claim (Test D)
# --------------------------------------------------------------------------- #
def test_low_trust_claim_does_not_inherit_high_story_trust():
    T = "2026-09-06T10:00:00+00:00"

    # 1. Establish a historically reliable story with a strong TIER_1 claim.
    with get_session() as s:
        _seed_source(s, id="src-tier1", name="Tier-One Official", tier="TIER_1")
        good_item = _seed_item(s, title="good", source_id="src-tier1")

    good_run = run_verification(claims_specs=[{
        "claim_id": "c-good", "text": "The tax is three euros.",
        "story_id": "story-d1", "source_item_ids": [good_item], "tiers": ["TIER_1"],
    }], story_id="story-d1", reference_time=T)

    good_score = good_run["trust_evaluations"][0]["trust_score"]
    assert good_score >= 60, f"strong TIER_1 claim should score high, got {good_score}"

    # 2. A brand-new low-trust rumor on a DIFFERENT story must NOT inherit that high trust.
    with get_session() as s:
        _seed_source(s, id="src-rumor", name="Social Rumor", tier="TIER_4")
        rumor_item = _seed_item(s, title="rumor", source_id="src-rumor")

    rumor_run = run_verification(claims_specs=[{
        "claim_id": "c-rumor", "text": "The minister is accused of corruption.",
        "story_id": "story-d2", "source_item_ids": [rumor_item], "tiers": ["TIER_4"],
    }], story_id="story-d2", reference_time=T)

    rumor_score = rumor_run["trust_evaluations"][0]["trust_score"]
    # The new claim scores on its own weak evidence — it is NOT boosted to the TIER_1 level.
    assert rumor_score < 60, f"low-trust claim must not inherit high story trust, got {rumor_score}"
    assert rumor_score < good_score
    assert rumor_run["decision"] != "PUBLISH", (
        f"low-trust claim must not auto-publish despite a reliable story existing: {rumor_run['decision']}"
    )


# --------------------------------------------------------------------------- #
# Test E — determinism: identical input + reference_time -> identical output (Test E)
# --------------------------------------------------------------------------- #
def test_pipeline_is_deterministic_across_runs():
    T = "2026-09-06T10:00:00+00:00"

    specs = [
        {"claim_id": "c-det-1", "text": "The tax is three euros.",
         "story_id": "story-e", "source_item_ids": ["det-item-a"], "tiers": ["TIER_2"]},
        {"claim_id": "c-det-2", "text": "Prices rose this quarter.",
         "story_id": "story-e", "source_item_ids": ["det-item-b"], "tiers": ["TIER_2"]},
    ]

    # Stable claim_ids (explicit) + injected reference_time make the run reproducible.
    r1 = run_verification(claims_specs=specs, story_id="story-e", reference_time=T)
    r2 = run_verification(claims_specs=specs, story_id="story-e", reference_time=T)

    assert r1 == r2, "identical input and reference_time must yield identical trust/quality/risk/decision"


# --------------------------------------------------------------------------- #
# Bypass protection (regression): dangerous inputs can never PUBLISH via the pipeline (#6)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "The minister is accused of embezzling public funds.",   # RED (allegation)
    "A court case against the CEO has been filed.",          # RED (court case)
    "Emergency shutdown ordered after cyberattack.",         # RED (emergency/cyberattack)
    "The mayor faces an investigation into a crime.",        # RED (crime/investigation)
])
def test_no_bypass_path_can_publish_dangerous_content(text):
    with get_session() as s:
        _seed_source(s, id="src-x", name="X Outlet", tier="TIER_1")
        item = _seed_item(s, title="x", source_id="src-x")

    result = run_verification(claims_specs=[{
        "claim_id": f"c-bypass-{text[:6]}", "text": text,
        "story_id": "my-story-1", "source_item_ids": [item], "tiers": ["TIER_1"],
    }], story_id="my-story-1")

    assert result["decision"] != "PUBLISH"


# --------------------------------------------------------------------------- #
# G5 — audit trail end-to-end: run_verification leaves a policy-tagged audit record
# --------------------------------------------------------------------------- #
def test_audit_trail_is_written_with_policy_version():
    with get_session() as s:
        _seed_source(s, id="src-audit", name="Audit Outlet", tier="TIER_1")
        item = _seed_item(s, title="ok", source_id="src-audit")

    run_verification(claims_specs=[{
        "claim_id": "c-audit", "text": "The tax is three euros.",
        "story_id": "my-story-1", "source_item_ids": [item], "tiers": ["TIER_1"],
    }], story_id="my-story-1")

    with get_session() as s:
        row = s.query(audit_logs).first()
        assert row is not None, "run_verification must write an audit_log entry"
        assert row.actor == "verify-engine"
        assert "VERIFICATION_DECISION" in row.action
        assert row.policy_version == "p3.v1"
        # before/after carry the decision context used for auditing (§14)
        after = from_jsonable(row.after_json)
        assert after.get("decision") in {"PUBLISH", "REVIEW", "WAIT", "REJECT", "UPDATE"}


# --------------------------------------------------------------------------- #
# G3 — provenance end-to-end: Story -> Source -> Source Item -> Claim -> Evidence
# --------------------------------------------------------------------------- #
def test_provenance_chain_reconstructed_from_db():
    T = "2026-09-06T10:00:00+00:00"

    with get_session() as s:
        _seed_source(s, id="src-prov", name="Provenance Source", tier="TIER_2")
        item = _seed_item(s, title="the-article", source_id="src-prov",
                          published_at="2026-09-05T10:00:00+00:00")
        _seed_story(s, story_id="my-story-prov")

    run_verification(claims_specs=[{
        "claim_id": "c-prov", "text": "The government approved the measure.",
        "story_id": "my-story-prov", "source_item_ids": [item], "tiers": ["TIER_2"],
    }], story_id="my-story-prov", reference_time=T)

    # A closed session from seeding must not be reused; open a fresh one for the audit query.
    with get_session() as s:
        chain = build_provenance_chain(s, "my-story-prov")

    assert chain["story"] is not None and chain["story"]["title"] == "My Story"
    assert len(chain["claims"]) == 1
    claim = chain["claims"][0]

    # Story -> Source Item -> Claim
    assert claim["item_title"] == "the-article"
    assert claim["item_published_at"] == "2026-09-05T10:00:00+00:00"
    assert claim["text"] == "The government approved the measure."

    # Source Item -> Source (with tier)
    assert claim["source_name"] == "Provenance Source"
    assert claim["source_tier"] == "TIER_2"
    # `item` is already the source item id string returned by _seed_item.
    assert claim["evidence"][0]["source_item_id"] == item

    # The full chain must answer every auditor question end-to-end.
    src = claim["evidence"][0]
    assert src["item_title"] == "the-article"
    assert chain["story"]["story_id"] == "my-story-prov"


# --------------------------------------------------------------------------- #
# H2 — a new conflicting claim cannot inherit a historical verified claim's validity
# --------------------------------------------------------------------------- #
def test_new_conflicting_info_cannot_inherit_old_verified_claim():
    """H2: old verified claim + new conflicting information -> WAIT/REVIEW, never PUBLISH."""
    T = "2026-09-06T10:00:00+00:00"

    with get_session() as s:
        _seed_source(s, id="src-hist", name="Official Source", tier="TIER_1")
        _seed_item(s, title="approval-article", source_id="src-hist", published_at="2026-09-05T10:00:00+00:00")
        _seed_item(s, title="rejection-article", source_id="src-hist", published_at="2026-09-06T10:00:00+00:00")
        old_item = s.query(source_items).filter_by(title="approval-article").first()
        new_item = s.query(source_items).filter_by(title="rejection-article").first()

    specs = [
        {"claim_id": "c-old-approved", "text": "The government approved the measure.",
         "story_id": "s2", "source_item_ids": [str(old_item.id)], "tiers": ["TIER_1"]},
        {"claim_id": "c-new-rejected", "text": "The government rejected the measure.",
         "story_id": "s2", "source_item_ids": [str(new_item.id)], "tiers": ["TIER_1"]},
    ]

    result = run_verification(claims_specs=specs, story_id="s2", reference_time=T)

    # The new conflicting information must not silently inherit the old claim's validity.
    assert result["decision"] != "PUBLISH"
    assert result["decision"] in ("WAIT", "REVIEW"), f"conflicting info published: {result['decision']}"
    # The contradiction must be surfaced, never hidden behind a high-trust story.
    assert result["has_contradiction"] is True
    assert "contradiction_detected" in (result["quality_reasons"] or []), result["quality_reasons"]


# --------------------------------------------------------------------------- #
# H3 — a high source tier cannot validate an unsupported claim
# --------------------------------------------------------------------------- #
def test_high_tier_source_cannot_validate_unsupported_claim():
    """H3: high source tier + critical claim but no evidence -> WAIT/REVIEW/REJECT, never PUBLISH."""
    T = "2026-09-06T10:00:00+00:00"

    with get_session() as s:
        _seed_source(s, id="src-authoritative", name="Reuters-like", tier="TIER_1")

    # The claim asserts a TIER_1 origin (tiers=["TIER_1"]) yet carries NO backing source item.
    specs = [
        {"claim_id": "c-unbacked", "text": "The council approved the infrastructure plan.",
         "story_id": "s3", "tiers": ["TIER_1"], "source_item_ids": []},
    ]

    result = run_verification(claims_specs=specs, story_id="s3", reference_time=T)

    assert result["decision"] != "PUBLISH"
    assert result["quality_reasons"] == ["unsupported_claim"], result["quality_reasons"]


# --------------------------------------------------------------------------- #
# Idempotency integration: re-running the same evaluation creates no duplicates (#7)
# --------------------------------------------------------------------------- #
def test_pipeline_is_idempotent_on_rerun():
    T = "2026-09-06T10:00:00+00:00"
    specs = [
        {"claim_id": "c-idem", "text": "The tax is three euros.",
         "story_id": "my-story-1", "source_item_ids": ["idem-item"], "tiers": ["TIER_2"]},
    ]

    run_verification(claims_specs=specs, story_id="my-story-1", reference_time=T)
    run_verification(claims_specs=specs, story_id="my-story-1", reference_time=T)

    with get_session() as s:
        assert s.query(claims).count() == 1, "duplicate claims must not be created"
        assert s.query(claim_evidence).count() == 1, "duplicate evidence must not be created"
        assert s.query(decisions).count() == 1, "duplicate decisions must not be created"
        assert s.query(trust_evaluations).count() == 1, "duplicate trust evaluations must not be created"
        assert s.query(quality_evaluations).count() == 1, "duplicate quality evaluations must not be created"
        # The audit trail is intentionally append-only (one event per run) and therefore consistent:
        # the entities above are idempotent, while each execution appends its own audit record.
        assert s.query(audit_logs).count() == 2, "audit log should have one entry per run"

