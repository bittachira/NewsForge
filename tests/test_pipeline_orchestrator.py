"""Pipeline Orchestrator tests (P1-P6).

Every test drives the REAL ``run_pipeline`` against an isolated database and
verifies both the returned structured result AND the persisted database rows.
No component is mocked except where explicitly noted (Destination, AiRouter).
"""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import newsforge.db as db
from newsforge.db import (
    get_session, publications, publication_attempts, generated_artifacts,
    ai_jobs, ai_runs, analytics, stories, story_signals, source_items,
)
from newsforge.pipeline.orchestrator import (
    PipelinePhaseError, StoryOutcome, run_pipeline, _default_claim_spec_builder,
)
from newsforge.publish.destinations import Destination, DistributionOutcome


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
    from newsforge.publish.destinations import register_builtin_destinations, reset_registry
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
    src.country = "ES"
    src.language = "es"
    src.tier = tier
    src.trust_score = 90 if tier == "TIER_1" else 35
    src.status = "active"
    session.add(src)
    session.commit()
    return id


def _seed_item(session, *, source_id: str, title: str, description: str | None = None,
               published_at: str = "2026-09-05T10:00:00+00:00") -> str:
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


# --------------------------------------------------------------------------- #
# A — Happy path: detect -> verify -> PUBLISH -> generate -> publish -> measure
# --------------------------------------------------------------------------- #
def test_happy_path_full_pipeline():
    """A supported TIER_1 claim must flow through all phases to PUBLISHED."""
    with get_session() as s:
        _seed_source(s, id="src-ok", name="Official", tier="TIER_1")
        item = _seed_item(s, source_id="src-ok", title="The tax is three euros.")

    result = run_pipeline(signal_ids=[item], reference_time="2026-09-05T12:00:00+00:00")

    assert result["stories_detected"] >= 1
    assert result["stories_processed"] >= 1
    outcome = result["outcomes"][0]

    assert outcome.final_status == "PUBLISHED", f"expected PUBLISHED, got {outcome.final_status}: {outcome.error}"
    assert outcome.decision is not None
    assert outcome.decision["decision"] == "PUBLISH"
    assert outcome.artifact is not None
    assert outcome.publish is not None
    assert outcome.publish["published"] is True
    assert outcome.measurement is not None

    # Verify DB: publication row exists (keyed by the story BUSINESS key)
    with get_session() as s:
        pubs = s.query(publications).filter_by(story_id=outcome.business_key).all()
        assert len(pubs) >= 1
    for p in pubs:
        assert str(p.status) == "COMPLETED"
        for p in pubs:
            assert str(p.status) == "COMPLETED"

    # Verify DB: AI cost recorded
    with get_session() as s:
        runs = s.query(ai_runs).filter_by(run_id=outcome.artifact["artifact_id"]).all()
        assert len(runs) == 1
        job = s.query(ai_jobs).filter_by(id=runs[0].job_id).first()
        assert job is not None
        assert job.cost_usd >= 0


# --------------------------------------------------------------------------- #
# B — REJECT: RED risk -> decision REJECT -> story blocked
# --------------------------------------------------------------------------- #
def test_reject_blocks_publication():
    """A RED-risk claim with unsupported claims must produce REJECT and no publication."""
    with get_session() as s:
        _seed_source(s, id="src-red", name="Rumor-Outlet", tier="TIER_4")
        item = _seed_item(s, source_id="src-red",
                          title="The minister is accused of embezzling public funds.")

    # Custom builder: RED risk claim WITHOUT evidence -> unsupported -> REJECT
    def reject_builder(session, story_handle, business_key, *, reference_time=None):
        return [{
            "claim_id": "reject-claim-1",
            "text": "The minister is accused of embezzling public funds.",
            "story_id": story_handle,
            "source_item_ids": [],  # no evidence -> unsupported
            "tiers": ["TIER_4"],
        }]

    result = run_pipeline(
        signal_ids=[item],
        reference_time="2026-09-05T12:00:00+00:00",
        claim_spec_builder=reject_builder,
    )

    assert result["stories_processed"] >= 1
    outcome = result["outcomes"][0]

    assert outcome.final_status == "REJECT", f"expected REJECT, got {outcome.final_status}"
    assert outcome.decision["decision"] != "PUBLISH"
    assert outcome.artifact is None
    assert outcome.publish is None

    # Verify DB: no publications
    with get_session() as s:
        assert s.query(publications).count() == 0


# --------------------------------------------------------------------------- #
# C — WAIT: unsupported claims -> WAIT -> story blocked
# --------------------------------------------------------------------------- #
def test_wait_blocks_publication():
    """Claims without evidence must produce WAIT and no publication."""
    with get_session() as s:
        _seed_source(s, id="src-wait", name="Somewhat Reliable", tier="TIER_2")
        item = _seed_item(s, source_id="src-wait", title="The government approved the measure.")

    # Use a custom claim spec builder that produces a claim with NO source_item_ids
    # (unlike the default builder which links evidence).
    def no_evidence_builder(session, story_handle, business_key, *, reference_time=None):
        return [{
            "claim_id": "wait-claim-1",
            "text": "The government approved the measure.",
            "story_id": story_handle,
            "source_item_ids": [],  # no evidence
            "tiers": ["TIER_2"],
        }]

    result = run_pipeline(
        signal_ids=[item],
        reference_time="2026-09-05T12:00:00+00:00",
        claim_spec_builder=no_evidence_builder,
    )

    assert result["stories_processed"] >= 1
    outcome = result["outcomes"][0]
    assert outcome.final_status == "WAIT", f"expected WAIT, got {outcome.final_status}"
    assert outcome.artifact is None
    assert outcome.publish is None

    with get_session() as s:
        assert s.query(publications).count() == 0


# --------------------------------------------------------------------------- #
# D — Idempotent rerun: same inputs -> same result, no duplicate rows
# --------------------------------------------------------------------------- #
def test_idempotent_rerun():
    """Running the same pipeline twice must not create duplicate publications
    or AI cost rows."""
    with get_session() as s:
        _seed_source(s, id="src-idem", name="Idempotent Source", tier="TIER_1")
        item = _seed_item(s, source_id="src-idem", title="The tax is three euros.")

    ref = "2026-09-05T12:00:00+00:00"
    run1 = run_pipeline(signal_ids=[item], reference_time=ref, destinations=["recording"])
    run2 = run_pipeline(signal_ids=[item], reference_time=ref, destinations=["recording"])

    o1 = run1["outcomes"][0]
    o2 = run2["outcomes"][0]

    assert o1.final_status == "PUBLISHED"
    assert o2.final_status == "PUBLISHED"
    assert o1.story_handle == o2.story_handle

    # No duplicate publications
    with get_session() as s:
        pubs = s.query(publications).filter_by(story_id=o1.business_key).all()
        assert len(pubs) == 1  # exactly one publication, not two

    # No duplicate AI runs
    with get_session() as s:
        runs = s.query(ai_runs).filter_by(run_id=o1.artifact["artifact_id"]).all()
        assert len(runs) == 1

    # Artifact not duplicated
    assert o1.artifact["artifact_id"] == o2.artifact["artifact_id"]
    assert o2.artifact["created"] is False  # second run reused existing


# --------------------------------------------------------------------------- #
# E — AI cost propagation: generated artifact records ai_jobs + ai_runs
# --------------------------------------------------------------------------- #
def test_ai_cost_recorded():
    """generate_story must record ai_jobs and ai_runs for every published artifact."""
    with get_session() as s:
        _seed_source(s, id="src-cost", name="Cost Source", tier="TIER_1")
        item = _seed_item(s, source_id="src-cost", title="The tax is three euros.")

    result = run_pipeline(signal_ids=[item], reference_time="2026-09-05T12:00:00+00:00")
    outcome = result["outcomes"][0]

    assert outcome.artifact is not None
    artifact_id = outcome.artifact["artifact_id"]

    with get_session() as s:
        runs = s.query(ai_runs).filter_by(run_id=artifact_id).all()
        assert len(runs) == 1
        job = s.query(ai_jobs).filter_by(id=runs[0].job_id).first()
        assert job is not None
        assert job.model_provider == "mock"
        assert job.tokens_input > 0 or job.tokens_output > 0
        assert job.cost_usd >= 0


# --------------------------------------------------------------------------- #
# F — Analytics event creation: traffic + revenue observations
# --------------------------------------------------------------------------- #
def test_analytics_events_recorded():
    """When traffic/revenue observations are provided, analytics rows are created."""
    with get_session() as s:
        _seed_source(s, id="src-analytics", name="Analytics Source", tier="TIER_1")
        item = _seed_item(s, source_id="src-analytics", title="The tax is three euros.")

    result = run_pipeline(
        signal_ids=[item],
        reference_time="2026-09-05T12:00:00+00:00",
        traffic_observations=[{"views": 100, "users": 50, "recorded_at": "2026-09-05T13:00:00+00:00"}],
        revenue_observations=[{"amount": 1.23, "currency": "EUR", "recorded_at": "2026-09-05T13:00:00+00:00"}],
    )
    outcome = result["outcomes"][0]
    assert outcome.analytics is not None
    assert outcome.analytics["traffic"] == 1
    assert outcome.analytics["revenue"] == 1

    with get_session() as s:
        traffic_rows = s.query(analytics).filter_by(entity_id=outcome.business_key, metric="traffic").all()
        assert len(traffic_rows) >= 1
        revenue_rows = s.query(analytics).filter_by(entity_id=outcome.business_key, metric="revenue").all()
        assert len(revenue_rows) >= 1


# --------------------------------------------------------------------------- #
# G — Destination failure: publisher records FAILED attempt
# --------------------------------------------------------------------------- #
def test_destination_failure_recorded():
    """When a destination fails, the publisher records a FAILED attempt
    and the outcome reflects BLOCKED (not a pipeline crash)."""
    class FailingDestination(Destination):
        key = "failing"
        name = "Always Fails"
        type = "WEBSITE"

        def publish(self, payload=None):
            return DistributionOutcome(succeeded=False, error="simulated failure")

    # Register only the failing destination
    from newsforge.publish.destinations import register, reset_registry, register_builtin_destinations
    reset_registry()
    register("failing", FailingDestination)

    with get_session() as s:
        _seed_source(s, id="src-fail", name="Fail Source", tier="TIER_1")
        item = _seed_item(s, source_id="src-fail", title="The tax is three euros.")

    result = run_pipeline(
        signal_ids=[item],
        reference_time="2026-09-05T12:00:00+00:00",
        destinations=["failing"],
        register_defaults=False,
    )
    outcome = result["outcomes"][0]

    # Decision was PUBLISH but destination failed
    assert outcome.decision["decision"] == "PUBLISH"
    assert outcome.publish is not None
    assert outcome.publish["published"] is False

    # FAILED attempt recorded
    with get_session() as s:
        attempts = s.query(publication_attempts).all()
        failed = [a for a in attempts if str(a.status) == "FAILED"]
        assert len(failed) >= 1


# --------------------------------------------------------------------------- #
# H — No publication before decision (REJECT case)
# --------------------------------------------------------------------------- #
def test_no_publish_before_decision():
    """When decision is WAIT/REJECT, no publication rows must exist at all."""
    with get_session() as s:
        _seed_source(s, id="src-pbd", name="PBD Source", tier="TIER_4")
        item = _seed_item(s, source_id="src-pbd",
                          title="The minister is accused of embezzling public funds.")

    result = run_pipeline(signal_ids=[item], reference_time="2026-09-05T12:00:00+00:00")
    outcome = result["outcomes"][0]

    assert outcome.final_status in ("REJECT", "WAIT")
    assert outcome.publish is None
    assert outcome.artifact is None

    with get_session() as s:
        assert s.query(publications).count() == 0
        assert s.query(generated_artifacts).count() == 0


# --------------------------------------------------------------------------- #
# I — No publication after WAIT
# --------------------------------------------------------------------------- #
def test_no_publish_after_wait():
    """When decision is WAIT, no publication or generation must occur."""
    def wait_builder(session, story_handle, business_key, *, reference_time=None):
        return [{
            "claim_id": "wait-only",
            "text": "Something happened.",
            "story_id": story_handle,
            "source_item_ids": [],
            "tiers": ["TIER_2"],
        }]

    with get_session() as s:
        _seed_source(s, id="src-wait2", name="Wait Source", tier="TIER_2")
        item = _seed_item(s, source_id="src-wait2", title="Something happened.")

    result = run_pipeline(
        signal_ids=[item],
        reference_time="2026-09-05T12:00:00+00:00",
        claim_spec_builder=wait_builder,
    )
    outcome = result["outcomes"][0]

    assert outcome.final_status == "WAIT"
    assert outcome.artifact is None
    assert outcome.publish is None

    with get_session() as s:
        assert s.query(publications).count() == 0
        assert s.query(generated_artifacts).count() == 0


# --------------------------------------------------------------------------- #
# J — Partial failure: one destination OK, one fails
# --------------------------------------------------------------------------- #
def test_partial_destination_failure():
    """When one destination succeeds and one fails, the overall publish
    is partial and the outcome reflects this."""
    class FailingDest(Destination):
        key = "fail-partial"
        name = "Fail Partial"
        type = "WEBSITE"

        def publish(self, payload=None):
            return DistributionOutcome(succeeded=False, error="partial failure")

    from newsforge.publish.destinations import register, reset_registry
    reset_registry()
    # Register both recording (always succeeds) and failing
    from newsforge.publish.destinations import register_builtin_destinations
    register_builtin_destinations()
    register("fail-partial", FailingDest)

    with get_session() as s:
        _seed_source(s, id="src-partial", name="Partial Source", tier="TIER_1")
        item = _seed_item(s, source_id="src-partial", title="The tax is three euros.")

    result = run_pipeline(
        signal_ids=[item],
        reference_time="2026-09-05T12:00:00+00:00",
        destinations=["recording", "fail-partial"],
    )
    outcome = result["outcomes"][0]

    assert outcome.decision["decision"] == "PUBLISH"
    assert outcome.publish is not None
    # recording succeeds, fail-partial fails -> published is False (all must succeed)
    assert outcome.publish["published"] is False

    per_dest = outcome.publish["per_destination"]
    assert per_dest["recording"]["succeeded"] is True
    assert per_dest["fail-partial"]["succeeded"] is False


# --------------------------------------------------------------------------- #
# K — Blocked stories (no claim specs -> no items with text)
# --------------------------------------------------------------------------- #
def test_blocked_no_claim_specs():
    """When items have no text content, the story is BLOCKED with no claim specs."""
    with get_session() as s:
        _seed_source(s, id="src-empty", name="Empty Source", tier="TIER_1")
        # Item with no description and empty title won't produce claims
        item = _seed_item(s, source_id="src-empty", title="",
                          description=None)

    # Use the default builder; the item has no text, so specs will be empty
    result = run_pipeline(signal_ids=[item], reference_time="2026-09-05T12:00:00+00:00")

    # Either BLOCKED (no specs) or the item is simply not detected
    if result["stories_processed"] >= 1:
        outcome = result["outcomes"][0]
        assert outcome.final_status == "BLOCKED"
        assert outcome.publish is None


# --------------------------------------------------------------------------- #
# L — Multiple stories: different topics produce separate outcomes
# --------------------------------------------------------------------------- #
def test_multiple_stories_independent():
    """Stories from different items should be processed independently."""
    with get_session() as s:
        _seed_source(s, id="src-multi-1", name="Source A", tier="TIER_1")
        item_a = _seed_item(s, source_id="src-multi-1", title="The tax is three euros.")
        _seed_source(s, id="src-multi-2", name="Source B", tier="TIER_4")
        item_b = _seed_item(s, source_id="src-multi-2",
                            title="The minister is accused of embezzling public funds.")

    result = run_pipeline(
        signal_ids=[item_a, item_b],
        reference_time="2026-09-05T12:00:00+00:00",
    )

    # At least 2 stories detected (different topics)
    assert result["stories_detected"] >= 2
    statuses = {o.final_status for o in result["outcomes"]}
    # At least one should be PUBLISHED (the TIER_1 story)
    assert "PUBLISHED" in statuses


# --------------------------------------------------------------------------- #
# M — Custom claim spec builder override
# --------------------------------------------------------------------------- #
def test_custom_claim_spec_builder():
    """A custom claim_spec_builder is used instead of the default."""
    custom_builder = MagicMock(return_value=[{
        "claim_id": "custom-1",
        "text": "Custom claim text.",
        "story_id": "handled-by-builder",
        "source_item_ids": [],
        "tiers": ["TIER_1"],
    }])

    with get_session() as s:
        _seed_source(s, id="src-custom", name="Custom Source", tier="TIER_1")
        item = _seed_item(s, source_id="src-custom", title="Some item.")

    result = run_pipeline(
        signal_ids=[item],
        reference_time="2026-09-05T12:00:00+00:00",
        claim_spec_builder=custom_builder,
    )

    # The custom builder was called (it takes 3 positional + keyword args)
    assert custom_builder.call_count == 1
    call_args = custom_builder.call_args
    assert call_args[0][2] is not None  # business_key arg


# --------------------------------------------------------------------------- #
# N — E2E: full deterministic pipeline from detection to measurement
# --------------------------------------------------------------------------- #
def test_e2e_deterministic_pipeline():
    """Complete E2E: seed source + items -> detect -> verify -> PUBLISH -> generate
    -> publish -> measurement, fully deterministic with MOCK."""
    T = "2026-09-05T12:00:00+00:00"

    with get_session() as s:
        _seed_source(s, id="src-e2e", name="E2E Official", tier="TIER_1")
        i1 = _seed_item(s, source_id="src-e2e", title="The tax is three euros.",
                        published_at="2026-09-05T10:00:00+00:00")
        i2 = _seed_item(s, source_id="src-e2e", title="Prices rose this quarter.",
                        published_at="2026-09-05T11:00:00+00:00")

    run1 = run_pipeline(signal_ids=[i1, i2], reference_time=T)
    run2 = run_pipeline(signal_ids=[i1, i2], reference_time=T)

    for run in (run1, run2):
        assert run["stories_detected"] >= 1
        assert run["stories_processed"] >= 1
        o = run["outcomes"][0]
        assert o.final_status == "PUBLISHED"
        assert o.decision["decision"] == "PUBLISH"
        assert o.artifact is not None
        assert o.artifact["state"] == "VALIDATED"
        assert o.publish["published"] is True
        assert o.measurement is not None

    # Deterministic: same artifact_id on both runs
    assert run1["outcomes"][0].artifact["artifact_id"] == run2["outcomes"][0].artifact["artifact_id"]

    # Verify provenance chain resolves
    from newsforge.verify.persist import build_provenance_chain
    with get_session() as s:
        chain = build_provenance_chain(s, run1["outcomes"][0].story_handle)
        assert chain["story"] is not None
        assert len(chain["claims"]) >= 1


# --------------------------------------------------------------------------- #
# O — Error context: PipelinePhaseError carries phase + story_id
# --------------------------------------------------------------------------- #
def test_pipeline_phase_error_context():
    """PipelinePhaseError must carry phase name and context for debugging."""
    err = PipelinePhaseError(phase="TEST_PHASE", story_id="s-1", context="something broke")
    assert "TEST_PHASE" in str(err)
    assert "s-1" in str(err)
    assert "something broke" in str(err)
    assert err.phase == "TEST_PHASE"
    assert err.story_id == "s-1"
