"""P5 — Measurement (section 27).

This module turns the REAL P4 artifacts (:class:`publications`,
:class:`publication_attempts`) into deterministic, idempotent measurements. It is a pure
*observer*: it only READS the editorial tables and writes to the measurement/snapshot/event
tables. It never mutates ``decisions``, ``trust_evaluations``, ``quality_evaluations``,
``articles`` or ``stories`` and never re-derives an editorial verdict (§11).

Guarantees (all enforced in code, all tested):

* **Derived, not duplicated.** Every metric is computed from the real attempt/publication rows;
  nothing is fabricated. A repeated operation collapses to a single row per key via a UNIQUE
  constraint + ``IntegrityError`` rejection (§17 idempotency).
* **Deterministic / injectable clock.** ``reference_time`` (ISO string or ``datetime``) is the
  single clock for every measurement and snapshot. When omitted, it is derived from the real
  ``published_at``/``created_at`` timestamps of the artifacts, so the result is still reproducible
  given the same database state (§15).
* **Independent destinations.** Destinations are aggregated separately; a failure on one channel
  never contaminates another's metrics (§24).

Public API: :func:`record_publication_metrics`, :func:`record_destination_metrics`,
:func:`capture_snapshot`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError

from newsforge.db.models import (
    PublicationStatus,
    to_jsonable,
    destination_metrics,
    publication_attempts,
    publication_metrics,
    published_snapshots,
    publications,
    stories,
)
from newsforge.db.session import get_session


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp into a timezone-aware datetime (UTC assumed when naive)."""
    if value is None:
        return None
    dt = datetime.fromisoformat(value)  # 3.11+ handles offsets and bare datetimes
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_ts(dt: Any) -> str:
    """Format a datetime/ISO-string to second-precision ISO (matches the :func:`ts` schema width)."""
    if isinstance(dt, str):
        return dt
    if dt is None:
        return ""
    return dt.isoformat(timespec="seconds")


def _effective_reference_time(
    session, *, story_id: str, destination_key: Optional[str] = None, reference_time: Optional[str] = None
) -> Optional[str]:
    """The single clock for a measurement.

    Injected ``reference_time`` wins (deterministic tests). Otherwise it is derived from the real
    artifact timestamps so production measurements are anchored to when the content was published."""
    if reference_time is not None:
        return _fmt_ts(reference_time)
    pubs = session.query(publications).filter_by(story_id=str(story_id))
    if destination_key is not None:
        pubs = pubs.filter_by(destination_key=destination_key)
    rows = list(pubs.all())
    if not rows:
        return None
    earliest = min(
        (p.published_at or p.created_at) for p in rows if p.published_at or p.created_at
    )
    return _fmt_ts(earliest)


def _publication_attempts(session, publication_ids):
    """Return attempt rows for the given publication ids."""
    if not publication_ids:
        return []
    pub_ids = [str(pid) for pid in publication_ids]
    return list(
        session.query(publication_attempts).filter(publication_attempts.publication_id.in_(pub_ids)).all()
    )


def _latency_ms(distributed_at: Optional[str], reference_time: str) -> float:
    """Elapsed milliseconds from the effective clock to a distributed attempt (never negative)."""
    dist = _parse_ts(distributed_at)
    if dist is None:
        return 0.0
    ref_dt = _parse_ts(reference_time) or datetime.now(timezone.utc)
    return max(0.0, (dist - ref_dt).total_seconds() * 1000.0)


def _attempt_rollup(attempts, *, reference_time: str) -> dict:
    """Deterministic per-destination rollup of ``publication_attempts`` (section 27).

    Counts and latency are derived from the real attempt rows. Latency is anchored to the same
    effective clock used by :func:`record_publication_metrics`, so both metrics agree on a channel's
    timing (§15 determinism). Destinations never contaminate one another (§24)."""
    n_attempts = len(attempts)
    n_succeeded = sum(1 for a in attempts if str(a.status) == "SUCCEEDED")
    n_failed = n_attempts - n_succeeded
    latencies = [_latency_ms(a.distributed_at, reference_time) for a in attempts]
    min_lat = round(min(latencies), 3) if latencies else 0.0
    max_lat = round(max(latencies), 3) if latencies else 0.0
    avg_lat = round(sum(latencies) / len(latencies), 3) if latencies else 0.0
    success_rate = round(n_succeeded / n_attempts, 6) if n_attempts else 0.0
    return {
        "n_attempts": n_attempts,
        "n_succeeded": n_succeeded,
        "n_failed": n_failed,
        "avg_latency_ms": avg_lat,
        "min_latency_ms": min_lat,
        "max_latency_ms": max_lat,
        "success_rate": success_rate,
    }


def record_destination_metrics(session, *, story_id: str, reference_time: Optional[str] = None) -> dict:
    """Roll up a STORY's performance across destinations from the real artifacts (section 27).

    Aggregates every ``publications`` / ``publication_attempts`` row for ``story_id``, grouped by
    destination — a true rollup, NOT a copy of a single ``publication_metrics`` row. Each destination
    is aggregated independently so a failure on one channel never contaminates another's metrics (§15/§24).

    Idempotent by ``(story_id, reference_time)``: re-measuring collapses to the same rollup rows via
    the UNIQUE constraint. Derived only — never mutates editorial/verdict tables."""
    effective = _effective_reference_time(session, story_id=str(story_id), reference_time=reference_time)

    pubs = session.query(publications).filter_by(story_id=str(story_id)).all()
    rollups = []
    for pub in pubs:
        attempts = _publication_attempts(session, [pub.id])
        rollup = _attempt_rollup(attempts, reference_time=effective or "")
        row = destination_metrics(
            story_id=str(story_id),
            destination_key=str(pub.destination_key),
            reference_time=effective or "",
            total_publications=1,
            total_attempts=rollup["n_attempts"],
            n_succeeded=rollup["n_succeeded"],
            n_failed=rollup["n_failed"],
            avg_latency_ms=rollup["avg_latency_ms"],
            success_rate=rollup["success_rate"],
        )
        created = _idempotent_upsert(
            session, destination_metrics, **{**_column_values(row, destination_metrics), "reference_time": effective or ""}
        )
        rollups.append({
            "story_id": str(story_id),
            "destination_key": str(pub.destination_key),
            "reference_time": effective or "",
            "total_publications": 1,
            **rollup,
            "created": created,
        })

    # Capture the publish-time snapshot as part of observing this story (idempotent).
    capture_snapshot(session, story_id=str(story_id), reference_time=effective)

    return {
        "story_id": str(story_id),
        "reference_time": effective or "",
        "destinations": rollups,
        "destination_count": len(rollups),
    }




def _column_values(instance, model):
    """Return ``{column_name: value}`` for an ORM instance, excluding internal state keys.

    SQLAlchemy 2.0 mapped instances store ``_sa_instance_state`` in their ``__dict__``; the
    declarative constructor rejects it, so we project only real column names instead of spreading
    ``instance.__dict__`` (which would leak that key)."""
    return {c.name: getattr(instance, c.name) for c in model.__table__.columns}


def _idempotent_upsert(session, model, **fields):
    """Insert ``model(**fields)``; treat a UNIQUE-violation as an already-present no-op (§17)."""
    try:
        session.add(model(**fields))
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        return False




# --------------------------------------------------------------------------- #
# Snapshots (section 27)
# --------------------------------------------------------------------------- #
def _editorial_snapshot_fields(session, story_id):
    """Current EDITORIAL state of a story. READ-ONLY — never mutates the Story row."""
    story = session.query(stories).filter_by(story_id=str(story_id)).first()
    if story is None:
        return None
    # ``title``/``summary`` are NOT NULL on the snapshot row but may be NULL on the story;
    # coerce missing text to "" so the read-only observer never fails on a sparse story.
    return {
        "title": story.title or "",
        "summary": story.summary or "",
        "slug": story.slug,
        "status": str(story.status),
        "trust_score": int(getattr(story, "trust_score", 0) or 0),
    }


def _to_snapshot_dict(row) -> dict:
    return {
        "title": row.title,
        "summary": row.summary,
        "slug": row.slug,
        "status": str(row.status),
        "trust_score": int(row.trust_score or 0),
        "published_at": row.published_at,
    }


def capture_snapshot(session, *, story_id: str, reference_time: Optional[str] = None,
                    published_at: Optional[str] = None) -> dict:
    """Capture (idempotently) a snapshot of a published story's editorial state.

    Keyed by ``(story_id, reference_time)`` so re-capturing at the same clock is a no-op. Reads the
    CURRENT editorial fields; callers that want publish-time fidelity must measure BEFORE editing
    the story (the measurement layer captures on first observation). ``published_at``, when given,
    records WHEN the publication completed (persisted as-is — never recomputed). Never mutates
    editorial rows."""
    effective = _fmt_ts(reference_time) if reference_time is not None else ""
    existing = (
        session.query(published_snapshots)
        .filter_by(story_id=str(story_id), reference_time=effective)
        .first()
    )
    if existing is not None:
        return {"story_id": str(story_id), "reference_time": effective, "found": True, "snapshot": _to_snapshot_dict(existing)}

    fields = _editorial_snapshot_fields(session, story_id)
    if fields is None:
        return {"story_id": str(story_id), "reference_time": effective, "found": False}

    row = published_snapshots(
        story_id=str(story_id),
        reference_time=effective,
        title=fields["title"],
        summary=fields["summary"],
        slug=fields["slug"],
        status=fields["status"],
        trust_score=fields["trust_score"],
        published_at=_fmt_ts(published_at) if published_at is not None else None,
    )
    _idempotent_upsert(session, published_snapshots, **{**_column_values(row, published_snapshots), "reference_time": effective})
    return {"story_id": str(story_id), "reference_time": effective, "found": True, "snapshot": _to_snapshot_dict(row)}


# --------------------------------------------------------------------------- #
# Publication metrics (section 27) — granular, per channel
# --------------------------------------------------------------------------- #
def record_publication_metrics(
    session, *, story_id: str, destination_key: str, reference_time: Optional[str] = None
) -> dict:
    """Measure ONE publish operation to one channel from the real artifacts.

    Returns a structured measurement. Idempotent by ``(story_id, destination_key, reference_time)``;
    re-running collapses to a single row. Never mutates editorial/verdict tables."""
    dest = str(destination_key)
    effective = _effective_reference_time(session, story_id=story_id, destination_key=dest, reference_time=reference_time)

    pub = (
        session.query(publications)
        .filter_by(story_id=str(story_id), destination_key=dest)
        .first()
    )
    if pub is None:
        return {
            "story_id": str(story_id),
            "destination_key": dest,
            "reference_time": effective or "",
            "found": False,
            "n_attempts": 0,
            "n_succeeded": 0,
            "n_failed": 0,
            "success": False,
        }

    attempts = _publication_attempts(session, [pub.id])
    latencies = [_latency_ms(a.distributed_at, effective) for a in attempts]
    n_attempts = len(attempts)
    n_succeeded = sum(1 for a in attempts if str(a.status) == "SUCCEEDED")
    n_failed = sum(1 for a in attempts if str(a.status) != "SUCCEEDED")
    success = n_attempts > 0 and n_failed == 0

    min_lat = round(min(latencies), 3) if latencies else 0.0
    max_lat = round(max(latencies), 3) if latencies else 0.0
    avg_lat = round(sum(latencies) / len(latencies), 3) if latencies else 0.0

    first_at = attempts[0].distributed_at if attempts else None
    last_at = attempts[-1].distributed_at if attempts else None

    row = publication_metrics(
        story_id=str(story_id),
        destination_key=dest,
        reference_time=effective or "",
        n_attempts=n_attempts,
        n_succeeded=n_succeeded,
        n_failed=n_failed,
        latency_ms_min=min_lat,
        latency_ms_max=max_lat,
        latency_ms_avg=avg_lat,
        first_attempt_at=first_at,
        last_attempt_at=last_at,
        success=success,
    )
    created = _idempotent_upsert(session, publication_metrics, **_column_values(row, publication_metrics))

    # Capture the publish-time snapshot as part of observing this operation (idempotent).
    capture_snapshot(session, story_id=str(story_id), reference_time=effective)

    return {
        "story_id": str(story_id),
        "destination_key": dest,
        "reference_time": effective or "",
        "found": True,
        "n_attempts": n_attempts,
        "n_succeeded": n_succeeded,
        "n_failed": n_failed,
        "latency_ms_min": min_lat,
        "latency_ms_max": max_lat,
        "latency_ms_avg": avg_lat,
        "first_attempt_at": first_at,
        "last_attempt_at": last_at,
        "success": success,
        "created": created,
    }
