"""Secure publisher (section 24).

The publisher is the ONLY component allowed to move content out of NewsForge. It exists solely to
turn a *persisted* Decision Engine verdict into distribution across channels. Every safety rule lives
in :func:`newsforge.verify.persist.is_auto_publishable` and this module consumes it -- it never
re-derives trust, quality or risk (§11):

    Quality Gate  ->  Decision Engine  ->  persisted decision  ->  is_auto_publishable()  ->  publisher

Guarantees (all enforced in code, all tested):

* A story with no persisted decision can NEVER be published.
* A decision that is not ``PUBLISH`` (REVIEW / WAIT / REJECT / UPDATE) never publishes.
* ``human_override=True`` never auto-publishes.
* RED risk and unsupported critical claims never publish -- the Decision Engine refuses them, so the
  publisher suppresses them by consuming the verdict; it does not re-check anything (§11).
* A destination failure is isolated: it records a FAILED attempt for that channel and lets every other
  channel complete. It never corrupts the story's lifecycle (ArticleStatus / StoryStatus are untouched).
* Publishing is idempotent per ``(story_id, destination_key)`` via a stable hash key with a UNIQUE
  database constraint; retrying only re-attempts channels without a confirmed SUCCEEDED attempt.

The publisher writes ONLY to ``publications`` and ``publication_attempts`` -- it never mutates the
Story/Article rows that P1/P2 own, so distribution problems cannot affect editorial state (§24).
"""
from __future__ import annotations

import hashlib

from newsforge.db.models import (
    DecisionState,
    PublicationStatus,
    to_jsonable,
    decisions,
    publication_attempts,
    publications,
    stories,
)
from newsforge.db.session import get_session
from newsforge.verify.persist import is_auto_publishable
from .destinations import DistributionOutcome, get_destination, register_builtin_destinations
from .state import InvalidTransitionError, PublicationStateMachine


def idempotency_key(story_id: str, destination_key: str) -> str:
    """Stable SHA-256 identity of a ``(story_id, destination_key)`` pair.

    Deterministic and collision-free; enforced at the DB level by the UNIQUE constraint on
    ``publications.idempotency_key`` (§15)."""
    raw = f"{story_id}\x00{destination_key}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _load_decision(session, story_id):
    row = session.query(decisions).filter_by(target_type="STORY", target_id=str(story_id)).first()
    return row


def _should_attempt(session, publication, destination_key: str) -> bool:
    """True iff this channel has no confirmed SUCCEEDED attempt yet (fresh publish or retry-safe)."""
    row = session.query(publication_attempts).filter_by(
        publication_id=str(publication.id), destination_key=destination_key).first()
    return row is None or str(row.status) != "SUCCEEDED"


def _publication_payload(decision_row, story_id, destination_key):
    """Provenance-only payload. Deliberately NO trust/quality/risk fields -- those are consumed by the
    decision engine and recorded in ``decisions``; the publisher must not recompute them (§11)."""
    return {
        "story_id": str(story_id),
        "decision_id": str(decision_row.id),
        "decision": decision_row.decision,
        "destination_key": destination_key,
        "published_by": "newsforge-publisher",
    }


def _error_json(outcome: DistributionOutcome):
    return None if outcome.ok else {"error": outcome.error}


def _persist_attempt(session, publication, destination_key: str, outcome: DistributionOutcome) -> None:
    """Record one attempt for ``(publication, destination)``. Idempotent: retries UPDATE the existing
    row in place (never append), so re-runs do not duplicate attempts."""
    existing = session.query(publication_attempts).filter_by(
        publication_id=str(publication.id), destination_key=destination_key).first()
    if existing is not None:
        existing.status = "SUCCEEDED" if outcome.ok else "FAILED"
        existing.error_detail = to_jsonable(_error_json(outcome))
        session.add(existing)
        session.commit()
        return
    row = publication_attempts(
        publication_id=str(publication.id),
        destination_key=destination_key,
        status="SUCCEEDED" if outcome.ok else "FAILED",
        error_detail=to_jsonable(_error_json(outcome)),
        distributed_at=outcome.published_at,
    )
    _idempotent_add(session, row)


def _attempt_destination(session, publication, destination_key: str) -> DistributionOutcome:
    """Attempt one channel. ANY exception is caught and turned into a FAILED outcome so a broken
    destination can never crash the whole publish operation (§24)."""
    dest = get_destination(destination_key)
    if dest is None:
        return DistributionOutcome(succeeded=False, error=f"unknown destination {destination_key!r}")
    try:
        outcome = dest.publish(payload=_publication_payload(_decision_of(session, publication), publication.story_id,
                                                            destination_key))
    except Exception as exc:  # noqa: BLE001 - isolate ANY failure to this channel (§24)
        return DistributionOutcome(succeeded=False, error=f"{type(exc).__name__}: {exc}")
    _persist_attempt(session, publication, destination_key, outcome)
    return outcome


def _decision_of(session, publication):
    row = session.query(decisions).filter_by(id=publication.decision_id).first()
    return row


def _load_or_create_publication(session, story_id, destination_key: str, decision_row) -> publications | None:
    """Return the existing publication for ``(story, destination)``, or create a fresh PENDING one.

    The UNIQUE ``idempotency_key`` constraint makes a duplicate insert an IntegrityError (caught by
    :func:`_idempotent_add`) -- defence in depth on top of the explicit existence check below."""
    existing = session.query(publications).filter_by(
        story_id=str(story_id), destination_key=destination_key).first()
    if existing is not None:
        return existing
    pub = publications(
        story_id=str(story_id),
        destination_key=destination_key,
        decision_id=str(decision_row.id),
        status=PublicationStatus.PENDING.value,
        idempotency_key=idempotency_key(str(story_id), destination_key),
    )
    _idempotent_add(session, pub)
    return pub


def _finalize_publication(session, publication, *, succeeded_all: bool) -> None:
    """Move a publication to its terminal state for this run. Validated by the state machine.

    Idempotent: re-publishing an already-terminal row (e.g. a COMPLETED publication from a prior
    run of the same story/destination) is a no-op -- it must never raise on a self-transition (§10)."""
    target = PublicationStatus.COMPLETED.value if succeeded_all else PublicationStatus.FAILED.value
    if str(publication.status) == target:
        return
    PublicationStateMachine(publication).transition_to(target)
    # Persist WHEN this publication actually completed: the latest confirmed SUCCEEDED
    # attempt timestamp. Set once and never rewritten, so re-publishes keep a stable,
    # deterministic published_at (P5 gate 3/15). No new facts are invented — the value
    # comes straight from the recorded attempts.
    if target == PublicationStatus.COMPLETED.value and not publication.published_at:
        attempts = session.query(publication_attempts).filter_by(
            publication_id=str(publication.id), status="SUCCEEDED").all()
        stamps = [a.distributed_at for a in attempts if a.distributed_at]
        if stamps:
            publication.published_at = max(stamps)
    session.commit()


def _idempotent_add(session, instance):
    try:
        session.add(instance)
        session.commit()
        return True
    except Exception:  # noqa: BLE001 - UNIQUE constraint => already present, treat as no-op (§17)
        session.rollback()
        return False


def _resolve_destination_keys(destinations) -> list[str]:
    """Map destination instances or keys to their stable, de-duplicated keys.

    When ``destinations`` is ``None`` every registered key is used (sorted). Otherwise the caller
    supplies explicit destinations; an instance contributes its :attr:`Destination.key`, a bare
    string is treated as a key directly."""
    from newsforge.publish.destinations import available_keys

    keys: list[str] = []
    seen: set[str] = set()
    if destinations is None:
        for k in available_keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
        return sorted(keys)
    for d in destinations:
        key = getattr(d, "key", None) or str(d)
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return sorted(keys)


def _distribute(session, publications: dict[str, publications], decision_row) -> dict[str, bool]:
    """Attempt every publication that still lacks a confirmed SUCCEEDED attempt, then finalize all.

    Per-destination failures are isolated (each attempt is caught inside
    :func:`_attempt_destination`), so one broken channel cannot stop the others. The whole batch is
    finalized atomically to COMPLETED when *every* channel succeeded, otherwise FAILED."""
    key_succeeded: dict[str, bool] = {}
    for dk, pub in publications.items():
        if str(pub.status) == PublicationStatus.COMPLETED.value:
            # Already published (fresh publish or prior retry) -> do not re-attempt (§10).
            key_succeeded[dk] = True
            continue
        if _should_attempt(session, pub, dk):
            outcome = _attempt_destination(session, pub, dk)
            key_succeeded[dk] = outcome.ok
        else:
            # A SUCCEEDED attempt already exists for this channel -> treat as done.
            key_succeeded[dk] = True

    succeeded_all = bool(key_succeeded) and all(key_succeeded.values())
    for dk, pub in publications.items():
        _finalize_publication(session, pub, succeeded_all=succeeded_all)
    return key_succeeded


def _blocked_result(story_id, *, reason, human_override=False) -> dict:
    """Nothing was published. Always safe -- the caller must not treat this as success."""
    return {
        "story_id": story_id,
        "blocked": True,
        "reason": reason,
        "published": False,
        "publications": [],
        "per_destination": {},
        "human_override": human_override,
    }


def publish_story(session, *, story_id: str, destinations=None) -> dict:
    """Publish ``story_id`` to its channels ONLY if the Decision Engine already approved it.

    Safety contract (all enforced here by consuming the persisted verdict, never re-derived):

    * No persisted decision  -> blocked (nothing published).
    * Persisted decision is not ``PUBLISH`` or ``human_override=True`` -> blocked.
    * Trust/quality/risk are NOT recomputed: :func:`is_auto_publishable` already consumed them.

    Publishing writes ONLY to ``publications`` / ``publication_attempts``; the Story/Article rows
    stay untouched so a distribution failure can never corrupt editorial state (§24)."""
    decision_row = _load_decision(session, story_id)
    if decision_row is None:
        return _blocked_result(story_id, reason="no persisted decision for this story")

    human_override = bool(getattr(decision_row, "human_override", False))
    if not is_auto_publishable(decision_row):
        return _blocked_result(
            story_id,
            reason=f"decision {decision_row.decision!r} is not auto-publishable"
                   + (" (human_override=True)" if human_override else ""),
            human_override=human_override,
        )

    keys = _resolve_destination_keys(destinations)
    if not keys:
        return _blocked_result(story_id, reason="no destinations registered")

    publications: dict[str, publications] = {}
    for dk in keys:
        pub = _load_or_create_publication(session, story_id, dk, decision_row)
        if pub is not None:  # existing COMPLETED publication -> skip (idempotent, §9/§10)
            publications[dk] = pub

    key_succeeded = _distribute(session, publications, decision_row)
    succeeded_all = bool(key_succeeded) and all(key_succeeded.values())

    if succeeded_all:
        # Persist the publish-time editorial snapshot (P5, §27). The measurement layer is a
        # pure observer: it only READS editorial state and writes published_snapshots. It is
        # idempotent by (story_id, reference_time), so re-publishing at the same
        # published_at never duplicates the snapshot row.
        from newsforge.measurement import capture_snapshot

        stamps = [p.published_at for p in publications.values() if p.published_at]
        ref = max(stamps) if stamps else None
        capture_snapshot(session, story_id=str(story_id), reference_time=ref, published_at=ref)

    return {
        "story_id": story_id,
        "decision_id": str(decision_row.id),
        "blocked": False,
        "published": succeeded_all,
        "publications": [str(p.id) for p in publications.values()],
        "per_destination": {
            dk: {"succeeded": v, "status": str(publications[dk].status)}
            for dk, v in key_succeeded.items()
        },
    }


def retry_publication(session, *, publication_id: str) -> dict:
    """Safely re-attempt a previously FAILED publication without creating duplicate state.

    Only channels that still lack a confirmed SUCCEEDED attempt are touched; already-succeeded
    channels are skipped. The publication row is reused (never recreated), so no duplicate
    publication state is ever produced (§10)."""
    from newsforge.publish.destinations import available_keys

    pub = session.get(publications, publication_id)
    if pub is None:
        return {"status": "not_found", "publication_id": publication_id}

    # Re-run the exact same distribution for this one publication's channel(s).
    keys = [pub.destination_key] + [k for k in available_keys() if k != pub.destination_key]
    pubs: dict[str, publications] = {pub.destination_key: pub}
    key_succeeded = _distribute(session, pubs, _decision_of(session, pub))

    return {
        "status": "completed" if all(key_succeeded.values()) else "failed",
        "publication_id": str(pub.id),
        "per_destination": {
            dk: {"succeeded": v, "status": str(pubs[dk].status)}
            for dk, v in key_succeeded.items()
        },
    }


def reconstruct_chain(session, *, publication_id: str) -> dict:
    """Rebuild the full provenance chain for one publication:

    Story -> Decision -> Publication -> Attempt(s) -> Result. Pure reads; returns a plain dict."""
    from newsforge.db.models import to_jsonable

    pub = session.get(publications, publication_id)
    if pub is None:
        return {"error": "publication not found", "chain": None}

    decision_row = _decision_of(session, pub)
    story_row = session.query(stories).filter_by(id=pub.story_id).first()
    attempts = list(
        session.query(publication_attempts).filter_by(publication_id=str(pub.id)).all()
    )

    return {
        "story": {"id": pub.story_id, "title": story_row.title if story_row else None},
        "decision": decision_row.decision if decision_row else None,
        "human_override": bool(getattr(decision_row, "human_override", False)) if decision_row else None,
        "publication": {
            "id": str(pub.id),
            "story_id": pub.story_id,
            "destination_key": pub.destination_key,
            "decision_id": pub.decision_id,
            "status": pub.status,
            "idempotency_key": pub.idempotency_key,
        },
        "attempts": [
            {
                "destination_key": a.destination_key,
                "status": a.status,
                "error_detail": to_jsonable(a.error_detail),
                "distributed_at": a.distributed_at,
            }
            for a in attempts
        ],
    }
