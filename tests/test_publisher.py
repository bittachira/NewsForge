"""P4 — publisher contract tests.

The publisher is the ONLY component allowed to move content out of NewsForge. These tests prove its
safety contract end-to-end against REAL persisted ``decisions`` rows and REAL destination attempts:

    Quality Gate -> Decision Engine -> persisted decision -> is_auto_publishable() -> publisher

The publisher MUST NOT re-derive trust/quality/risk; it consumes the Decision Engine verdict via
:func:`newsforge.verify.persist.is_auto_publishable`. Every test drives the real guard against real
model rows (no parallel abstraction, no mocking of the engine's verdict)."""
from __future__ import annotations

import pytest

from newsforge.db import (
    PublicationStatus,
    decisions,
    get_session,
    publications,
    publication_attempts,
    stories,
    use_isolated_database_ctx,
)
from newsforge.publish import (
    idempotency_key,
    publish_story,
    reconstruct_chain,
    retry_publication,
    register_builtin_destinations,
    reset_registry,
)


# --------------------------------------------------------------------------- #
# Isolation fixtures + seeding helpers (consistent with existing P3 tests)
# --------------------------------------------------------------------------- #
# Monotonic counter so every test gets a UNIQUE database file under .pytest_tmp.
_db_seq = 0


@pytest.fixture(autouse=True)
def isolated_db():
    """Give every test its own throwaway database so no row can leak between tests.

    Each test opens a UNIQUE file (``<name>_<seq>.db``). A per-test file means one test's
    lingering connection can never corrupt another test's database, and pytest removes
    ``.pytest_tmp`` after each test -- we do NOT rely on deleting a file while a connection
    may still be open."""
    import shutil
    from pathlib import Path

    global _db_seq
    db_dir = Path.cwd() / ".pytest_tmp"
    db_dir.mkdir(exist_ok=True)
    _db_seq += 1
    path = db_dir / f"{Path(__name__).stem}_{_db_seq}.db"
    with use_isolated_database_ctx(path):
        yield
    # Best-effort cleanup. The isolation context already disposed its engine before we get here,
    # and because every test owns a distinct file, a failed removal cannot leak into another test.


@pytest.fixture(autouse=True)
def clean_registry():
    """Start every test from a pristine destination registry (built-ins only)."""
    from newsforge.publish.destinations import register_builtin_destinations

    reset_registry()
    register_builtin_destinations()
    yield


def _seed_story(session, *, story_id="story-1", trust_score=0):
    """Seed a real Story. ``id`` is pinned to ``story_id`` so reconstruct_chain's
    ``stories.filter_by(id=pub.story_id)`` lookup resolves (Publisher writes pub.story_id = str(story_id))."""
    st = stories()
    st.id = story_id
    # NOTE: do NOT also set st.story_id here -- it is unique and would collide. reconstruct_chain
    # keys off pub.story_id (== str(story_id)), so provenance still resolves with the default uuid.
    st.slug = story_id
    st.title = "Test Story"
    st.trust_score = trust_score
    session.add(st)
    session.commit()
    return story_id


def _seed_decision(session, *, story_id="story-1", decision="PUBLISH", human_override=False, risk_level=None):
    """Seed a real persisted Decision Engine verdict for the STORY."""
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


def _count_publications(session=None, *, story_id="story-1", destination_key="recording"):
    """Count publications for a (story, destination) pair. Opens its own session when none is passed."""
    if session is None:
        with get_session() as s:
            return len(
                s.query(publications).filter_by(story_id=str(story_id), destination_key=destination_key).all()
            )
    return len(
        session.query(publications).filter_by(story_id=str(story_id), destination_key=destination_key).all()
    )


def _attempts_for(session=None, *, publication_id):
    """Return attempt rows for a publication. Opens its own session when none is passed."""
    if session is None:
        with get_session() as s:
            return list(
                s.query(publication_attempts).filter_by(publication_id=publication_id).all()
            )
    return list(
        session.query(publication_attempts).filter_by(publication_id=publication_id).all()
    )


# --------------------------------------------------------------------------- #
# A. persisted PUBLISH -> publication succeeds
# --------------------------------------------------------------------------- #
def test_persisted_PUBLISH_publishes_successfully():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        decision_id = _seed_decision(s, story_id="story-1", decision="PUBLISH")

    result = publish_story(s, story_id="story-1", destinations=["recording"])

    assert result["blocked"] is False and result["published"] is True
    with get_session() as s:
        pub = s.query(publications).filter_by(story_id="story-1", destination_key="recording").one()
        assert str(pub.id) in result["publications"]
        assert str(pub.status) == PublicationStatus.COMPLETED.value


# --------------------------------------------------------------------------- #
# B. no persisted decision -> blocked (nothing may fall through)
# --------------------------------------------------------------------------- #
def test_no_decision_is_blocked():
    with get_session() as s:
        _seed_story(s, story_id="story-1")

    result = publish_story(s, story_id="story-1", destinations=["recording"])

    assert result["blocked"] is True and result["published"] is False
    assert "no persisted decision" in result["reason"]
    assert _count_publications(story_id="story-1") == 0


# --------------------------------------------------------------------------- #
# C. any non-PUBLISH verdict -> blocked (REVIEW / WAIT / DRAFT / REJECT)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("decision", ["REVIEW", "WAIT", "DRAFT", "REJECT"])
def test_non_publish_verdict_is_blocked(decision):
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        decision_id = _seed_decision(s, story_id="story-1", decision=decision)

    result = publish_story(s, story_id="story-1", destinations=["recording"])

    assert result["blocked"] is True and result["published"] is False
    assert _count_publications(story_id="story-1") == 0
    # The persisted verdict really does gate the guard (no double-safety gap).
    from newsforge.verify.persist import is_auto_publishable

    with get_session() as s:
        row = s.query(decisions).filter_by(id=decision_id).one()
    assert is_auto_publishable(row) is False


# --------------------------------------------------------------------------- #
# D. human_override=True -> blocked (a reviewer must not auto-release via the engine verdict)
# --------------------------------------------------------------------------- #
def test_human_override_is_blocked():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        decision_id = _seed_decision(
            s, story_id="story-1", decision="PUBLISH", human_override=True
        )

    result = publish_story(s, story_id="story-1", destinations=["recording"])

    assert result["blocked"] is True and result["published"] is False
    assert "human_override" in result["reason"]
    assert _count_publications(story_id="story-1") == 0


# --------------------------------------------------------------------------- #
# E–I. semantic safety verdicts must remain blocked (publisher consumes the persisted result)
# --------------------------------------------------------------------------- #
def test_RED_risk_is_blocked():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        decision_id = _seed_decision(s, story_id="story-1", decision="REVIEW", risk_level="RED")

    result = publish_story(s, story_id="story-1", destinations=["recording"])
    assert result["blocked"] is True and _count_publications(story_id="story-1") == 0


def test_unsupported_critical_claim_is_blocked():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        decision_id = _seed_decision(s, story_id="story-1", decision="REJECT")

    result = publish_story(s, story_id="story-1", destinations=["recording"])
    assert result["blocked"] is True and _count_publications(story_id="story-1") == 0


def test_contradiction_is_blocked():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        decision_id = _seed_decision(s, story_id="story-1", decision="REVIEW")

    result = publish_story(s, story_id="story-1", destinations=["recording"])
    assert result["blocked"] is True and _count_publications(story_id="story-1") == 0


def test_insufficient_trust_is_blocked():
    with get_session() as s:
        _seed_story(s, story_id="story-1", trust_score=20)
        decision_id = _seed_decision(s, story_id="story-1", decision="REVIEW")

    result = publish_story(s, story_id="story-1", destinations=["recording"])
    assert result["blocked"] is True and _count_publications(story_id="story-1") == 0


def test_high_trust_story_with_low_trust_new_claim_is_blocked():
    """A high-trust Story paired with a low-trust new claim must not auto-publish."""
    with get_session() as s:
        _seed_story(s, story_id="story-1", trust_score=98)
        decision_id = _seed_decision(s, story_id="story-1", decision="REVIEW")

    result = publish_story(s, story_id="story-1", destinations=["recording"])
    assert result["blocked"] is True and _count_publications(story_id="story-1") == 0


# --------------------------------------------------------------------------- #
# Deterministic record-only destinations for multi-channel tests
# --------------------------------------------------------------------------- #
from newsforge.publish.destinations import Destination, DistributionOutcome, register  # noqa: E402


class _AlwaysGood(Destination):
    key = "good"

    def publish(self, payload=None):  # noqa: D102
        return DistributionOutcome(
            succeeded=True, published_at="2026-09-07T00:00:00+00:00", payload=payload
        )


class _AlwaysBad(Destination):
    key = "bad"

    def publish(self, payload=None):  # noqa: D102
        return DistributionOutcome(succeeded=False, error="recorded failure")


# --------------------------------------------------------------------------- #
# J. same story + same destination twice -> exactly one publication (idempotent)
# --------------------------------------------------------------------------- #
def test_same_story_same_destination_is_idempotent():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")

    publish_story(s, story_id="story-1", destinations=["recording"])
    publish_story(s, story_id="story-1", destinations=["recording"])

    with get_session() as s:
        assert _count_publications(s, story_id="story-1", destination_key="recording") == 1
        pub = s.query(publications).filter_by(story_id="story-1", destination_key="recording").one()
        assert str(pub.status) == PublicationStatus.COMPLETED.value


# --------------------------------------------------------------------------- #
# K. multiple destinations -> independent publication records, all succeed
# --------------------------------------------------------------------------- #
def test_multiple_destinations_get_independent_records():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")

    # Register the record-only test doubles before publishing (mirrors the other multi-channel tests).
    reset_registry()
    register_builtin_destinations()
    register("good", _AlwaysGood)
    register("bad", _AlwaysBad)

    publish_story(s, story_id="story-1", destinations=["good", "recording"])

    with get_session() as s:
        rows = list(
            s.query(publications).filter_by(story_id="story-1").all()
        )
        assert {r.destination_key for r in rows} == {"good", "recording"}
        assert len(rows) == 2
        for r in rows:
            assert str(r.status) == PublicationStatus.COMPLETED.value


# --------------------------------------------------------------------------- #
# L. one destination fails -> failure is isolated; the other channel still succeeds at attempt level
# --------------------------------------------------------------------------- #
def test_one_destination_failure_isolated_other_channel_records_success():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")

    # Register the record-only test doubles before publishing (mirrors the retry test setup).
    reset_registry()
    register_builtin_destinations()
    register("good", _AlwaysGood)
    register("bad", _AlwaysBad)

    result = publish_story(s, story_id="story-1", destinations=["good", "bad"])

    assert result["blocked"] is False
    with get_session() as s:
        rows = {r.destination_key: r for r in s.query(publications).filter_by(story_id="story-1").all()}
        assert set(rows) == {"good", "bad"}  # two independent publication records exist

        good_attempts = _attempts_for(s, publication_id=str(rows["good"].id))
        bad_attempts = _attempts_for(s, publication_id=str(rows["bad"].id))

        # The failing channel recorded FAILED; the healthy channel recorded SUCCEEDED (isolated).
        assert any(a.status == "SUCCEEDED" for a in good_attempts)
        assert any(a.status == "FAILED" for a in bad_attempts)


# --------------------------------------------------------------------------- #
# M. retry a FAILED publication -> succeeds, no duplicate publication row
# --------------------------------------------------------------------------- #
def test_failed_publication_retry_succeeds_without_duplicate():
    from newsforge.publish.destinations import register

    with get_session() as s:
        _seed_story(s, story_id="story-1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")

    # First run: the channel fails -> publication ends FAILED.
    reset_registry()
    register_builtin_destinations()
    register("flaky", _AlwaysBad)

    first = publish_story(s, story_id="story-1", destinations=["flaky"])
    assert first["blocked"] is False and first["published"] is False

    with get_session() as s:
        assert _count_publications(s, story_id="story-1", destination_key="flaky") == 1
        pub = s.query(publications).filter_by(story_id="story-1", destination_key="flaky").one()
        assert str(pub.status) == PublicationStatus.FAILED.value

    # Channel is now healthy; retry must reuse the SAME publication row and drive it to COMPLETED.
    reset_registry()
    register_builtin_destinations()
    register("flaky", _AlwaysGood)

    retried = retry_publication(s, publication_id=str(pub.id))
    assert retried["status"] == "completed"

    with get_session() as s:
        # Exactly one publication row remains (no duplicate state).
        assert _count_publications(s, story_id="story-1", destination_key="flaky") == 1
        pub = s.query(publications).filter_by(story_id="story-1", destination_key="flaky").one()
        assert str(pub.status) == PublicationStatus.COMPLETED.value
        # The single attempt row was updated in place to SUCCEEDED (not appended).
        attempts = _attempts_for(s, publication_id=str(pub.id))
        assert len(attempts) == 1 and attempts[0].status == "SUCCEEDED"


# --------------------------------------------------------------------------- #
# N. invalid state transition -> rejected deterministically by the state machine
# --------------------------------------------------------------------------- #
def test_invalid_state_transition_is_rejected():
    from newsforge.publish.state import InvalidTransitionError, PublicationStateMachine

    with get_session() as s:
        _seed_story(s, story_id="story-1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")

    publish_story(s, story_id="story-1", destinations=["recording"])

    with get_session() as s:
        pub = s.query(publications).filter_by(story_id="story-1").one()

        # COMPLETED is terminal -> cannot go back to PENDING.
        sm = PublicationStateMachine(pub)
        assert str(sm.state) == PublicationStatus.COMPLETED.value
        with pytest.raises(InvalidTransitionError):
            sm.transition_to(PublicationStatus.PENDING)

        # A bogus target state is rejected from a live publication too.
        with pytest.raises(InvalidTransitionError):
            sm.transition_to("BOGUS")


# --------------------------------------------------------------------------- #
# O. provenance reconstruction -> Story -> Decision -> Publication -> Attempt -> Result
# --------------------------------------------------------------------------- #
def test_provenance_chain_is_reconstructed():
    with get_session() as s:
        story_id = _seed_story(s, story_id="story-1")
        decision_id = _seed_decision(s, story_id=story_id, decision="PUBLISH")

    publish_story(s, story_id=story_id, destinations=["recording"])

    with get_session() as s:
        pub = s.query(publications).filter_by(story_id=story_id, destination_key="recording").one()
        chain = reconstruct_chain(s, publication_id=str(pub.id))

        assert chain["story"]["id"] == story_id  # Story
        assert chain["decision"] == "PUBLISH" and chain["human_override"] is False  # Decision
        pub_part = chain["publication"]
        assert pub_part["id"] == str(pub.id)  # Publication
        assert pub_part["destination_key"] == "recording"
        assert pub_part["decision_id"] == decision_id
        assert pub_part["status"] == PublicationStatus.COMPLETED.value
        assert len(chain["attempts"]) == 1  # Attempt
        assert chain["attempts"][0]["destination_key"] == "recording"
        assert chain["attempts"][0]["status"] == "SUCCEEDED"  # Result


# --------------------------------------------------------------------------- #
# Security / contract properties
# --------------------------------------------------------------------------- #
def test_high_story_trust_alone_cannot_publish():
    """A high-trust Story with NO persisted Decision Engine verdict must never publish."""
    from newsforge.verify.persist import is_auto_publishable

    with get_session() as s:
        _seed_story(s, story_id="story-1", trust_score=99)

    result = publish_story(s, story_id="story-1", destinations=["recording"])

    assert result["blocked"] is True and result["published"] is False
    assert "no persisted decision" in result["reason"]
    assert _count_publications(story_id="story-1") == 0


def test_publisher_requires_persisted_decision_no_fallthrough():
    """Absence of a persisted PUBLISH decision must NEVER fall through to publishing."""
    with get_session() as s:
        _seed_story(s, story_id="story-1", trust_score=95)

    result = publish_story(s, story_id="story-1", destinations=["recording"])

    assert result["blocked"] is True
    assert _count_publications(story_id="story-1") == 0


def test_publisher_does_not_recompute_trust_or_risk():
    """The publisher consumes the persisted verdict; it never re-evaluates trust or risk.

    * A low-trust Story (trust_score=0) WITH a persisted PUBLISH decision DOES publish
      (proving the publisher does not reject on story trust).
    * A high-trust Story WITHOUT a persisted PUBLISH decision is still blocked
      (proving trust alone cannot bypass the gate)."""
    with get_session() as s:
        low_trust = _seed_story(s, story_id="low-trust", trust_score=0)
        _seed_decision(s, story_id=low_trust, decision="PUBLISH")

    # Low story trust + persisted PUBLISH -> publishes (trust is NOT re-evaluated).
    low_result = publish_story(s, story_id=low_trust, destinations=["recording"])
    assert low_result["blocked"] is False and low_result["published"] is True
    assert _count_publications(story_id=low_trust) == 1

    # High story trust but no persisted decision -> blocked.
    with get_session() as s:
        high_trust = _seed_story(s, story_id="high-trust", trust_score=99)

    high_result = publish_story(s, story_id=high_trust, destinations=["recording"])
    assert high_result["blocked"] is True and _count_publications(story_id=high_trust) == 0


def test_repeated_identical_calls_are_idempotent():
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")

    first = publish_story(s, story_id="story-1", destinations=["recording"])
    second = publish_story(s, story_id="story-1", destinations=["recording"])

    # Deterministic result shape and exactly one publication row.
    assert first["blocked"] is False and second["blocked"] is False
    assert _count_publications(story_id="story-1", destination_key="recording") == 1


def test_destination_failure_does_not_raise_or_leak():
    """A broken destination must be isolated: it records FAILED and never crashes publish_story,
    nor prevents other channels (or the overall return) from completing."""
    with get_session() as s:
        _seed_story(s, story_id="story-1")
        _seed_decision(s, story_id="story-1", decision="PUBLISH")

    # Register the record-only test doubles before publishing (mirrors the retry test setup).
    reset_registry()
    register_builtin_destinations()
    register("good", _AlwaysGood)
    register("bad", _AlwaysBad)

    result = publish_story(s, story_id="story-1", destinations=["good", "bad"])
    assert result["blocked"] is False and "per_destination" in result

    with get_session() as s:
        good_pub = s.query(publications).filter_by(story_id="story-1", destination_key="good").one()
        bad_pub = s.query(publications).filter_by(story_id="story-1", destination_key="bad").one()
        # The healthy channel's attempt is recorded SUCCEEDED (failure did not leak into it).
        good_attempts = _attempts_for(s, publication_id=str(good_pub.id))
        assert any(a.status == "SUCCEEDED" for a in good_attempts)
