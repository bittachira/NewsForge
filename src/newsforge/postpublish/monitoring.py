"""P5 — Post-publish monitoring (section 28).

Observer-only layer. It READS the real P4 artifacts (:class:`publications`,
:class:`publication_attempts`) and the captured snapshots, and it writes ONLY to
``published_snapshots`` / ``postpublish_events``. It never mutates ``decisions``,
``trust_evaluations``, ``quality_evaluations``, ``articles`` or ``stories``, never publishes
or modifies content, and never re-derives an editorial verdict (§11, §24).

Every function takes an injectable ``reference_time`` (ISO string or ``datetime``) as its single
clock. Events are deterministic and idempotent: re-running a scan at the same clock collapses to a
single row per ``(event_type, story_id, reference_time)`` via a UNIQUE constraint (§15/§17).

A change/stale detection NEVER bypasses the Decision Engine — it only appends a review *signal*
(:class:`postpublish_events`) and returns structured data for a human or a reviewer to act on.
"""
from __future__ import annotations

import json as _json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError

from newsforge.db.models import (
    StoryStatus,
    decisions,
    postpublish_events,
    published_snapshots,
    publication_attempts,
    publications,
    stories,
    to_jsonable,
)
from newsforge.db.session import get_session


# --------------------------------------------------------------------------- #
# Clock + tiny helpers (kept local so this module stays decoupled from measurement)
# --------------------------------------------------------------------------- #
def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp into a timezone-aware datetime (UTC assumed when naive)."""
    if value is None or value == "":
        return None
    dt = datetime.fromisoformat(value)  # 3.11+ handles offsets and bare datetimes
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_ts(dt: Any) -> str:
    """Format a datetime/ISO-string to second-precision ISO (matches the ``ts`` schema width)."""
    if isinstance(dt, str):
        return dt
    if dt is None:
        return ""
    return dt.isoformat(timespec="seconds")


def _effective_reference_time(session, *, story_id: Optional[str] = None, reference_time: Optional[str] = None) -> str:
    """The single clock for a post-publish scan.

    Injected ``reference_time`` wins (deterministic tests). Otherwise it is derived from the real
    artifact timestamps when a story is given; when nothing anchors the clock we return "" so the
    caller gets an empty result rather than a nondeterministic one (§15)."""
    if reference_time is not None:
        return _fmt_ts(reference_time)
    if story_id is not None:
        story = session.get(stories, str(story_id))
        if story is not None and (story.updated_at or story.created_at):
            ts = min(_parse_ts(story.updated_at) or datetime.min.replace(tzinfo=timezone.utc),
                     _parse_ts(story.created_at) or datetime.min.replace(tzinfo=timezone.utc))
            return _fmt_ts(ts)
    return ""


def _idempotent_add(session, instance) -> bool:
    """Insert one ORM instance; treat a UNIQUE-violation as an already-present no-op (§17)."""
    try:
        session.add(instance)
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        return False


def _editorial_fields(session, story) -> dict:
    """Current EDITORIAL state of a story. READ-ONLY — never mutates the Story row."""
    return {
        "title": story.title,
        "summary": story.summary,
        "slug": story.slug,
        "status": str(story.status),
        "trust_score": int(getattr(story, "trust_score", 0) or 0),
    }


def _diff(base: dict, current: dict) -> dict:
    """Keys whose values differ between the stored snapshot and the current editorial state."""
    diff = {}
    for key in base:
        if base[key] != current.get(key):
            diff[key] = {"was": base[key], "now": current.get(key)}
    return diff


# Explicit, deterministic stale policy (section 28). A published story is stale when its most
# recent content change predates ``max_age_hours`` before the reference clock. Tunable via a dict;
# defaults are fixed so results are reproducible given the same state + clock (§15).
DEFAULT_STALE_POLICY: dict = {"max_age_hours": 24.0}


def _policy_max_age_hours(policy: Optional[dict]) -> float:
    if not policy:
        return float(DEFAULT_STALE_POLICY["max_age_hours"])
    try:
        return float(policy["max_age_hours"])
    except (KeyError, TypeError):
        return float(DEFAULT_STALE_POLICY["max_age_hours"])


# --------------------------------------------------------------------------- #
# 1. Change detection — compare current editorial state vs stored snapshots (§28)
# --------------------------------------------------------------------------- #
def detect_changes(session, *, story_id: Optional[str] = None, reference_time: Optional[str] = None) -> dict:
    """Detect stories whose EDITORIAL state drifted after publication (section 28).

    Compares the current Story fields against the snapshot captured at ``reference_time``. For each
    changed story it appends a deterministic, idempotent ``CHANGE_DETECTED`` event — the review
    signal — and returns the structured diff. Never mutates editorial rows; never publishes (§11/§24)."""
    effective = _effective_reference_time(session, story_id=story_id, reference_time=reference_time)

    if story_id is not None:
        stories_to_check = [session.get(stories, str(story_id))]
    else:
        snapshot_rows = session.query(published_snapshots).all()
        stories_to_check = [session.get(stories, s.story_id) for s in snapshot_rows]

    changed = []
    for story in stories_to_check:
        if story is None:
            continue
        snap = (
            session.query(published_snapshots)
            .filter_by(story_id=str(story.id), reference_time=effective)
            .first()
        )
        if snap is None:
            continue
        diff = _diff(_to_snapshot_dict(snap), _editorial_fields(session, story))
        if diff:
            reason = f"editorial fields drifted after publication ({_json.dumps(diff, sort_keys=True, default=str)})"
            event = postpublish_events(
                event_type="CHANGE_DETECTED",
                story_id=str(story.id),
                decision_id=None,
                publication_id=None,
                reference_time=effective,
                reason=reason,
                payload_json=to_jsonable({"diff": diff}),
            )
            _idempotent_add(session, event)
            changed.append(
                {
                    "story_id": str(story.id),
                    "reference_time": effective,
                    "diff": diff,
                }
            )

    return {"story_id": story_id, "reference_time": effective, "changed": changed}


def _to_snapshot_dict(row) -> dict:
    return {
        "title": row.title,
        "summary": row.summary,
        "slug": row.slug,
        "status": str(row.status),
        "trust_score": int(row.trust_score or 0),
    }


# --------------------------------------------------------------------------- #
# 2. Stale detection — published stories past the freshness window (§28)
# --------------------------------------------------------------------------- #
def detect_stale(session, *, reference_time: Optional[str] = None, policy: Optional[dict] = None) -> dict:
    """Detect published stories that have gone stale per an explicit, deterministic policy (§28).

    A story is STALE when it has been published AND its most recent content change (``updated_at``,
    falling back to ``created_at``) predates ``max_age_hours`` before the reference clock. Each stale
    story appends a deterministic, idempotent ``STALE_DETECTED`` event and returns structured data.

    Never publishes or modifies anything; it only signals for review (§11/§24)."""
    max_age = _policy_max_age_hours(policy)
    effective = _effective_reference_time(session, story_id=None, reference_time=reference_time)
    ref_dt = _parse_ts(effective) if effective else None

    stale = []
    if ref_dt is not None:
        cutoff = ref_dt - timedelta(hours=max_age)
        pubs = session.query(publications).all()
        for pub in pubs:
            story = session.get(stories, pub.story_id)
            if story is None:
                continue
            # Only ACTIVE published content can drift into staleness.
            if str(story.status) != StoryStatus.ACTIVE.value:
                continue
            updated = _parse_ts(story.updated_at) or _parse_ts(story.created_at)
            if updated is not None and updated <= cutoff:
                event = postpublish_events(
                    event_type="STALE_DETECTED",
                    story_id=str(story.id),
                    decision_id=None,
                    publication_id=str(pub.id),
                    reference_time=effective,
                    reason=f"published content not refreshed for {max_age:g}h before reference time",
                    payload_json=to_jsonable(
                        {"story_id": str(story.id), "destination_key": pub.destination_key,
                         "updated_at": str(story.updated_at), "cutoff": str(cutoff)}
                    ),
                )
                _idempotent_add(session, event)
                stale.append(
                    {
                        "story_id": str(story.id),
                        "destination_key": pub.destination_key,
                        "reference_time": effective,
                        "updated_at": str(story.updated_at),
                        "cutoff": str(cutoff),
                    }
                )

    return {"reference_time": effective, "policy": policy or dict(DEFAULT_STALE_POLICY), "stale": stale}


# --------------------------------------------------------------------------- #
# 3. Needs-update signal — register a review flag WITHOUT publishing/modifying (§28)
# --------------------------------------------------------------------------- #
def mark_needing_update(session, *, story_id: str, reason: Optional[str] = None,
                        reference_time: Optional[str] = None) -> dict:
    """Register that ``story_id`` needs review because it changed or went stale after publication.

    This ONLY appends a deterministic, idempotent ``NEEDS_UPDATE`` event — a review signal for a
    human or the Decision Engine to act on. It NEVER publishes an update and NEVER modifies content,
    decisions, trust/quality/artcles/stories (§11/§24)."""
    effective = _effective_reference_time(session, story_id=str(story_id), reference_time=reference_time)
    event = postpublish_events(
        event_type="NEEDS_UPDATE",
        story_id=str(story_id),
        decision_id=None,
        publication_id=None,
        reference_time=effective,
        reason=reason or "Content changed or went stale after publication; requires review before re-publishing.",
        payload_json=to_jsonable({"story_id": str(story_id), "reference_time": effective}),
    )
    created = _idempotent_add(session, event)
    return {"story_id": str(story_id), "reference_time": effective, "created": created}


# --------------------------------------------------------------------------- #
# 4. Provenance reconstruction — Story -> Decision -> Publication -> Attempt -> Outcome (§29)
# --------------------------------------------------------------------------- #
def reconstruct_provenance(session, *, story_id: Optional[str] = None, publication_id: Optional[str] = None,
                           reference_time: Optional[str] = None) -> dict:
    """Rebuild the real provenance chain for one or more publications (§29).

    Chain order: Story -> Decision -> Publication -> Attempt(s) -> Outcome. Reads ONLY the real P4
    artifacts (stories / decisions / publications / publication_attempts) plus any post-publish
    events logged for the story. Pure reads; returns plain dicts. Never mutates anything."""
    if publication_id is not None:
        pubs = [session.get(publications, str(publication_id))]
    elif story_id is not None:
        pubs = session.query(publications).filter_by(story_id=str(story_id)).all()
    else:
        return {"error": "story_id or publication_id required", "reference_time": _fmt_ts(reference_time), "chains": []}

    effective = _effective_reference_time(session, story_id=story_id, reference_time=reference_time)
    chains = []
    for pub in pubs:
        decision_row = session.query(decisions).filter_by(id=pub.decision_id).first()
        story_row = session.get(stories, pub.story_id) if pub.story_id else None
        attempts = list(
            session.query(publication_attempts).filter_by(publication_id=str(pub.id)).all()
        )

        attempts_outcome = [
            {
                "destination_key": a.destination_key,
                "status": str(a.status),
                "error_detail": a.error_detail,
                "distributed_at": a.distributed_at,
            }
            for a in attempts
        ]

        events_rows = list(
            session.query(postpublish_events).filter_by(story_id=pub.story_id).all()
        )
        events_outcome = [
            {
                "event_type": e.event_type,
                "reference_time": e.reference_time,
                "reason": e.reason,
            }
            for e in events_rows
        ]

        chains.append(
            {
                "story": (
                    {"id": pub.story_id, "title": story_row.title if story_row else None,
                     "slug": story_row.slug if story_row else None}
                    if story_row else None
                ),
                "decision": (
                    {"id": str(decision_row.id), "decision": decision_row.decision,
                     "risk_level": decision_row.risk_level,
                     "human_override": bool(getattr(decision_row, "human_override", False))}
                    if decision_row else None
                ),
                "publication": {
                    "id": str(pub.id),
                    "story_id": pub.story_id,
                    "destination_key": pub.destination_key,
                    "status": pub.status,
                    "decision_id": pub.decision_id,
                },
                "attempts": attempts_outcome,
                "outcome": {
                    dk: {"status": str(a.status), "error_detail": a.error_detail,
                         "distributed_at": a.distributed_at}
                    for dk, a in zip(
                        [a.destination_key for a in attempts], attempts
                    )
                },
                "postpublish_events": events_outcome,
            }
        )

    return {
        "story_id": story_id,
        "publication_id": publication_id,
        "reference_time": effective,
        "chains": chains,
    }


__all__ = [
    "detect_changes",
    "detect_stale",
    "mark_needing_update",
    "reconstruct_provenance",
    "DEFAULT_STALE_POLICY",
]
