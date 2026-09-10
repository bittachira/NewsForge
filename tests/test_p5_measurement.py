"""P5 — Measurement + post-publish monitoring tests (section 27/28).

Every test runs against a throwaway SQLite database (isolated per test) with its own session. No row
leaks between tests and no `.db` file is reused (§15 isolation). Destinations are record-only so
behaviour is fully deterministic (§15)."""
from __future__ import annotations

import pytest

from newsforge.db import (
    decisions,
    get_session,
    postpublish_events,
    published_snapshots,
    publications,
    publication_metrics,
    destination_metrics,
    stories,
    use_isolated_database_ctx,
)
from newsforge.measurement import (
    capture_snapshot,
    record_destination_metrics,
    record_publication_metrics,
)
from newsforge.postpublish import (
    DEFAULT_STALE_POLICY,
    detect_changes,
    detect_stale,
    mark_needing_update,
    reconstruct_provenance,
)
from newsforge.publish import publish_story, register_builtin_destinations, reset_registry
from newsforge.publish.destinations import Destination, DistributionOutcome, register
from newsforge.verify.persist import is_auto_publishable


# --------------------------------------------------------------------------- #
# Isolation fixtures + seeding helpers (consistent with existing P3/P4 tests)
# --------------------------------------------------------------------------- #
_db_seq = 0


@pytest.fixture(autouse=True)
def isolated_db():
    """Give every test its own throwaway database so no row can leak between tests."""
    import shutil
    from pathlib import Path

    global _db_seq
    db_dir = Path.cwd() / ".pytest_tmp"
    db_dir.mkdir(exist_ok=True)
    _db_seq += 1
    # Module-unique namespace: __name__ is dotted (tests.test_p5_measurement), so
    # Path(__name__).stem would be "tests" and collide with other modules sharing that
    # stem. Use the final component, and never reuse a stale file (unlink; bump the
    # sequence if it is locked) so runs are independent of .pytest_tmp state and order.
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
    """Start every test from a pristine destination registry (built-ins only)."""
    reset_registry()
    register_builtin_destinations()
    register("good", _AlwaysGood)
    register("bad", _AlwaysBad)
    yield


class _AlwaysGood(Destination):
    key = "good"

    def publish(self, payload=None):  # noqa: D102
        return DistributionOutcome(succeeded=True, published_at=T0, payload=payload)


class _AlwaysBad(Destination):
    key = "bad"

    def publish(self, payload=None):  # noqa: D102
        return DistributionOutcome(succeeded=False, error="recorded failure")


def _seed_story(session, *, story_id="story-1", summary="Summary v1", status="ACTIVE", trust_score=0):
    """Seed a real Story with a non-null summary so snapshots insert cleanly.

    story_id is BOTH the stories.id PK and the business key stories.story_id
    (tests run with the PK==business-key convention; §business-key coherence)."""
    st = stories()
    st.id = story_id
    st.story_id = story_id
    st.slug = story_id
    st.title = "Test Story"
    st.summary = summary
    st.status = status
    st.trust_score = trust_score
    session.add(st)
    session.commit()
    return story_id


def _seed_decision(session, *, story_id="story-1", decision="PUBLISH", human_override=False):
    """Seed a real persisted Decision Engine verdict for the STORY."""
    row = decisions(target_type="STORY", target_id=story_id, decision=decision,
                    human_override=human_override)
    session.add(row)
    session.commit()
    return str(row.id)


def _pin_updated_at(session, story_id, value):
    """Set a Story's updated_at to an explicit value, bypassing the onupdate callable."""
    session.query(stories).filter_by(id=story_id).update({"updated_at": value})
    session.commit()


def _count(session, model, **filters):
    q = session.query(model)
    for k, v in filters.items():
        q = q.filter_by(**{k: str(v)})
    return len(q.all())


def _edit_story_field(session, story_id, field, value):
    st = session.get(stories, story_id)
    setattr(st, field, value)
    session.commit()


def _last_decision_id():
    with get_session() as s:
        return str(s.query(decisions).order_by(decisions.id.desc()).first().id)


# Reference clock used across tests (fixed, so latency is deterministic).
T0 = "2026-09-07T00:00:00+00:00"  # == RecordingDestination distributed_at -> zero latency


# --------------------------------------------------------------------------- #
# 1. successful publication -> correct metrics
# --------------------------------------------------------------------------- #
def test_publication_success_metrics_correct():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")

    # P4 publishes first; metrics then measure the REAL publication/attempts rows.
    publish_story(s, story_id="story-1", destinations=["recording"])

    result = record_publication_metrics(s, story_id="story-1", destination_key="recording", reference_time=T0)
    assert result["found"] is True
    assert result["n_attempts"] == 1
    assert result["n_succeeded"] == 1
    assert result["n_failed"] == 0
    assert result["success"] is True
    assert result["reference_time"] == T0


def test_publication_metrics_row_is_persisted():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")

    publish_story(s, story_id="story-1", destinations=["recording"])

    record_publication_metrics(s, story_id="story-1", destination_key="recording", reference_time=T0)
    with get_session() as s:
        row = s.query(publication_metrics).filter_by(story_id="story-1", destination_key="recording").one()
        assert str(row.id) is not None


# --------------------------------------------------------------------------- #
# 2. failed publication -> correct metrics
# --------------------------------------------------------------------------- #
def test_publication_failure_metrics_correct():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")

    result = publish_story(s, story_id="story-1", destinations=["bad"])
    assert result["blocked"] is False and result["published"] is False

    with get_session() as s:
        m = record_publication_metrics(s, story_id="story-1", destination_key="bad", reference_time=T0)
        assert m["found"] is True
        assert m["n_attempts"] == 1
        assert m["n_succeeded"] == 0
        assert m["n_failed"] == 1
        assert m["success"] is False


# --------------------------------------------------------------------------- #
# 3. multiple attempts -> correct aggregation across destinations
# --------------------------------------------------------------------------- #
def test_multiple_destinations_aggregation_correct():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")

    publish_story(s, story_id="story-1", destinations=["good", "recording"])

    with get_session() as s:
        rollup = record_destination_metrics(s, story_id="story-1", reference_time=T0)
        assert rollup["destination_count"] == 2
        by_key = {r["destination_key"]: r for r in rollup["destinations"]}
        assert set(by_key) == {"good", "recording"}
        for dk, r in by_key.items():
            assert r["n_attempts"] == 1 and r["n_succeeded"] == 1 and r["success_rate"] == 1.0


# --------------------------------------------------------------------------- #
# 4. idempotency of metrics (respects the UNIQUE constraints)
# --------------------------------------------------------------------------- #
def test_metrics_are_idempotent():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")

    publish_story(s, story_id="story-1", destinations=["recording"])

    r1 = record_publication_metrics(s, story_id="story-1", destination_key="recording", reference_time=T0)
    r2 = record_publication_metrics(s, story_id="story-1", destination_key="recording", reference_time=T0)
    assert r1["created"] is True and r2["created"] is False
    with get_session() as s:
        assert _count(s, publication_metrics, story_id="story-1", destination_key="recording") == 1


def test_destination_metrics_are_idempotent():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")

    publish_story(s, story_id="story-1", destinations=["recording"])

    record_destination_metrics(s, story_id="story-1", reference_time=T0)
    record_destination_metrics(s, story_id="story-1", reference_time=T0)
    with get_session() as s:
        assert _count(s, destination_metrics, story_id="story-1") == 1


# --------------------------------------------------------------------------- #
# 5. independent destinations (a failure on one never contaminates another)
# --------------------------------------------------------------------------- #
def test_destinations_are_independent():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")

    publish_story(s, story_id="story-1", destinations=["good", "bad"])

    with get_session() as s:
        rollup = record_destination_metrics(s, story_id="story-1", reference_time=T0)
        by_key = {r["destination_key"]: r for r in rollup["destinations"]}
        assert by_key["good"]["n_succeeded"] == 1 and by_key["good"]["success_rate"] == 1.0
        assert by_key["bad"]["n_succeeded"] == 0 and by_key["bad"]["success_rate"] == 0.0


# --------------------------------------------------------------------------- #
# 6. deterministic snapshot + no change when content is untouched
# --------------------------------------------------------------------------- #
def test_snapshot_is_deterministic_and_changeless_when_untouched():
    with get_session() as s:
        _seed_story(s, story_id="story-1", summary="Summary v1")

    a = capture_snapshot(s, story_id="story-1", reference_time=T0)
    b = capture_snapshot(s, story_id="story-1", reference_time=T0)
    assert a["found"] is True and b["found"] is True
    assert a["snapshot"] == b["snapshot"]

    with get_session() as s:
        assert _count(s, published_snapshots, story_id="story-1", reference_time=T0) == 1

    changes = detect_changes(s, story_id="story-1", reference_time=T0)
    assert changes["changed"] == []


# --------------------------------------------------------------------------- #
# 7. change detection (editorial drift after publication)
# --------------------------------------------------------------------------- #
def test_change_detection_flags_editorial_drift():
    with get_session() as s:
        _seed_story(s, story_id="story-1", summary="Summary v1")
        capture_snapshot(s, story_id="story-1", reference_time=T0)

    _edit_story_field(s, "story-1", "title", "Edited Title")

    changes = detect_changes(s, story_id="story-1", reference_time=T0)
    assert len(changes["changed"]) == 1
    diff = changes["changed"][0]["diff"]
    assert "title" in diff and diff["title"]["was"] == "Test Story" and diff["title"]["now"] == "Edited Title"

    with get_session() as s:
        event = s.query(postpublish_events).filter_by(
            event_type="CHANGE_DETECTED", story_id="story-1", reference_time=T0
        ).one()
        assert event.event_type == "CHANGE_DETECTED"


def test_change_detection_is_idempotent():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        capture_snapshot(s, story_id="story-1", reference_time=T0)
        _edit_story_field(s, "story-1", "summary", "Edited Summary")

    detect_changes(s, story_id="story-1", reference_time=T0)
    detect_changes(s, story_id="story-1", reference_time=T0)
    with get_session() as s:
        events = s.query(postpublish_events).filter_by(event_type="CHANGE_DETECTED", story_id="story-1").all()
        assert len(events) == 1


# --------------------------------------------------------------------------- #
# 8. stale detection (explicit, deterministic policy)
# --------------------------------------------------------------------------- #
def test_story_stale_after_freshness_window():
    with get_session() as s:
        _seed_story(s, story_id="story-1", summary="v1")
        _pin_updated_at(s, "story-1", T0)
        _seed_decision(s, story_id="story-1", decision="PUBLISH")
        publish_story(s, story_id="story-1", destinations=["good"])

    stale = detect_stale(s, reference_time="2026-09-08T00:00:00+00:00", policy={"max_age_hours": 24})
    assert len(stale["stale"]) == 1
    with get_session() as s:
        event = s.query(postpublish_events).filter_by(event_type="STALE_DETECTED", story_id="story-1").one()
        assert event.event_type == "STALE_DETECTED"


def test_story_not_stale_within_window():
    with get_session() as s:
        _seed_story(s, story_id="story-1", summary="v1")
        _pin_updated_at(s, "story-1", T0)
        _seed_decision(s, story_id="story-1", decision="PUBLISH")
        publish_story(s, story_id="story-1", destinations=["good"])

    stale = detect_stale(s, reference_time="2026-09-07T12:00:00+00:00", policy={"max_age_hours": 24})
    assert stale["stale"] == []


# --------------------------------------------------------------------------- #
# 9. mark_needing_update registers a signal but never publishes/modifies
# --------------------------------------------------------------------------- #
def test_mark_needing_update_does_not_publish_or_modify():
    with get_session() as s:
        _seed_story(s, story_id="story-1", summary="v1")
        before = {f: getattr(s.get(stories, "story-1"), f) for f in ("title", "summary", "slug", "status")}
        pub_count_before = _count(s, publications, story_id="story-1")

    mark_needing_update(s, story_id="story-1", reason="manual flag", reference_time=T0)

    with get_session() as s:
        st = s.get(stories, "story-1")
        for f, v in before.items():
            assert getattr(st, f) == v  # editorial state untouched
        assert _count(s, publications, story_id="story-1") == pub_count_before  # nothing published

    with get_session() as s:
        event = s.query(postpublish_events).filter_by(event_type="NEEDS_UPDATE", story_id="story-1").one()
        assert event.event_type == "NEEDS_UPDATE"


# --------------------------------------------------------------------------- #
# 10. full provenance reconstruction Story -> Decision -> Publication -> Attempt -> Outcome
# --------------------------------------------------------------------------- #
def test_reconstruct_provenance_full_chain():
    with get_session() as s:
        _seed_story(s, story_id="story-1", summary="v1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")
        publish_story(s, story_id="story-1", destinations=["good"])

    chain = reconstruct_provenance(s, story_id="story-1", reference_time=T0)
    assert len(chain["chains"]) == 1
    c = chain["chains"][0]
    assert c["story"]["id"] == "story-1" and c["story"]["title"] == "Test Story"
    assert c["decision"]["decision"] == "PUBLISH"
    assert c["publication"]["status"] == "COMPLETED"
    assert len(c["attempts"]) == 1 and c["attempts"][0]["destination_key"] == "good"
    assert c["outcome"]["good"]["status"] == "SUCCEEDED"


# --------------------------------------------------------------------------- #
# 11. injected clock drives the measurements deterministically
# --------------------------------------------------------------------------- #
def test_injected_clock_controls_latency_and_reference_time():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")
        publish_story(s, story_id="story-1", destinations=["good"])

    at_t0 = record_publication_metrics(s, story_id="story-1", destination_key="good", reference_time=T0)
    before_dist = record_publication_metrics(s, story_id="story-1", destination_key="good",
                                            reference_time="2026-09-06T23:59:00+00:00")

    assert at_t0["reference_time"] == T0 and at_t0["latency_ms_avg"] == 0.0
    assert before_dist["reference_time"] == "2026-09-06T23:59:00+00:00"
    assert before_dist["latency_ms_avg"] == pytest.approx(60_000.0, abs=1.0)


# --------------------------------------------------------------------------- #
# 12. determinism with the same state + configuration
# --------------------------------------------------------------------------- #
def test_deterministic_with_same_state_and_config():
    """Same state + same clock => identical measurement results (idempotent)."""
    with get_session() as s:
        _seed_story(s, story_id="story-1", summary="v1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")
        publish_story(s, story_id="story-1", destinations=["good"])

    with get_session() as s:
        first = record_destination_metrics(s, story_id="story-1", reference_time=T0)
    with get_session() as s:
        second = record_destination_metrics(s, story_id="story-1", reference_time=T0)
    # Compare measurement values (strip idempotency flag: created=True/False differs by design)
    for d in first["destinations"]:
        d.pop("created", None)
    for d in second["destinations"]:
        d.pop("created", None)
    assert first == second


# --------------------------------------------------------------------------- #
# 13. P3/P4 regression: no bypass to PUBLISH; measurement is purely observational
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("decision", ["REVIEW", "WAIT", "REJECT"])
def test_non_publish_verdicts_stay_blocked(decision):
    """P5 must not be able to turn WAIT/REVIEW/REJECT into PUBLISH."""
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        _seed_decision(s, story_id="story-1", decision=decision)

    result = publish_story(s, story_id="story-1", destinations=["good"])
    assert result["blocked"] is True and result["published"] is False
    with get_session() as s:
        assert _count(s, publications, story_id="story-1") == 0
        row = s.query(decisions).filter_by(id=_last_decision_id()).one()
        # The single gate still gates the verdict (P5 never rewrites it).
        assert is_auto_publishable(row) is False


def test_measurement_never_changes_editorial_state():
    """Measurement + post-publish are observers: they never mutate editorial/verdict tables."""
    with get_session() as s:
        _seed_story(s, story_id="story-1", summary="v1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")
        publish_story(s, story_id="story-1", destinations=["good"])

    with get_session() as s:
        snapshot_state = {f: getattr(s.get(stories, "story-1"), f) for f in ("title", "summary", "slug", "status")}
        decision_before = s.query(decisions).filter_by(id=_last_decision_id()).one()

    with get_session() as s:
        record_publication_metrics(s, story_id="story-1", destination_key="good", reference_time=T0)
        capture_snapshot(s, story_id="story-1", reference_time=T0)
        detect_changes(s, story_id="story-1", reference_time=T0)
        detect_stale(s, reference_time=T0)
        mark_needing_update(s, story_id="story-1", reference_time=T0)
        reconstruct_provenance(s, story_id="story-1", reference_time=T0)

    with get_session() as s:
        st = s.get(stories, "story-1")
        for f, v in snapshot_state.items():
            assert getattr(st, f) == v  # Story untouched
        decision_after = s.query(decisions).filter_by(id=_last_decision_id()).one()
        assert decision_after.decision == decision_before.decision  # Decision untouched


def test_default_stale_policy_is_explicit():
    """The stale policy is a fixed, explicit default (no hidden/LLM/probabilistic logic)."""
    assert DEFAULT_STALE_POLICY == {"max_age_hours": 24.0}
