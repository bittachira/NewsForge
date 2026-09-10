"""P6 - Analytics event ingestion (section 28, section 48).

Writes into the EXISTING ``analytics`` table (entity_id / metric /
dimension_value / value / recorded_at). No new schema, no duplicated data:

* traffic:  metric="traffic" (views) and metric="users", dimension_value=None
* revenue:  metric="revenue", dimension_value=<currency> (default "USD")

Both recorders are idempotent on the natural key
``(entity_id, metric, dimension_value, recorded_at)``: re-recording the same
logical event is a no-op, so BI aggregates never double-count.

This layer only APPENDS measurement rows. It never touches editorial state
(stories/claims/decisions/trust/quality/publications).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from newsforge.db.models import analytics


def _fmt_ts(value: Optional[str]) -> str:
    """Normalize an ISO string / datetime to second-precision ISO (schema width 27)."""
    if value is None:
        return datetime.now().isoformat(timespec="seconds")
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _exists(session, *, entity_id: str, metric: str, dimension_value: Optional[str],
            recorded_at: str) -> bool:
    q = session.query(analytics).filter_by(
        entity_id=str(entity_id), metric=metric, recorded_at=recorded_at)
    if dimension_value is None:
        q = q.filter(analytics.dimension_value.is_(None))
    else:
        q = q.filter_by(dimension_value=dimension_value)
    return q.first() is not None


def record_traffic_event(session, *, entity_id: str, views: int = 0,
                         users: int = 0, recorded_at: Optional[str] = None) -> dict:
    """Record one traffic observation for an entity (story/article id).

    ``views`` is the primary traffic counter; ``users`` is optional. Rows are
    appended to ``analytics``; identical logical events are not duplicated.
    
    Uses flush() instead of commit() to allow visibility from subsequent queries
    in the same or different sessions when transactions are shared."""
    ts_ = _fmt_ts(recorded_at)
    created = 0
    if views:
        if not _exists(session, entity_id=entity_id, metric="traffic",
                       dimension_value=None, recorded_at=ts_):
            session.add(analytics(entity_id=str(entity_id), metric="traffic",
                                  dimension_value=None, value=float(views),
                                  recorded_at=ts_))
            created += 1
            session.flush()  # Make visible immediately
    if users:
        if not _exists(session, entity_id=entity_id, metric="users",
                       dimension_value=None, recorded_at=ts_):
            session.add(analytics(entity_id=str(entity_id), metric="users",
                                  dimension_value=None, value=float(users),
                                  recorded_at=ts_))
            created += 1
            session.flush()  # Make visible immediately
    return {"entity_id": str(entity_id), "recorded_at": ts_, "rows_created": created}


def record_revenue_event(session, *, entity_id: str, amount: float,
                         currency: str = "USD", recorded_at: Optional[str] = None) -> dict:
    """Record one revenue attribution for an entity.

    The currency is stored as ``dimension_value`` so BI can aggregate per
    currency without any hardcoded assumption. Identical logical events are
    not duplicated.
    
    Uses flush() instead of commit() to allow visibility from subsequent queries."""
    if amount < 0:
        raise ValueError("revenue amount must be >= 0")
    ts_ = _fmt_ts(recorded_at)
    created = 0
    if not _exists(session, entity_id=entity_id, metric="revenue",
                   dimension_value=currency, recorded_at=ts_):
        session.add(analytics(entity_id=str(entity_id), metric="revenue",
                              dimension_value=currency, value=float(amount),
                              recorded_at=ts_))
        created += 1
        session.flush()  # Make visible immediately
    return {"entity_id": str(entity_id), "recorded_at": ts_, "rows_created": created}