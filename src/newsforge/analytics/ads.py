"""Ad analytics events — strictly separated lifecycle tracking.

Tracks the full ad lifecycle WITHOUT assuming one event implies the next:

  slot_rendered  -> slot HTML inserted into article page (SSR, server-side)
  ad_request     -> ad request sent to provider (client-side JS)
  ad_loaded      -> ad creative loaded into slot (client-side JS)
  ad_impression  -> ad visible to user (viewability measurement)
  ad_click       -> user clicked ad (client-side JS)
  ad_revenue     -> revenue attributed (provider webhook/report)

Each event is recorded independently. The ABSENCE of a later event does NOT
invalidate an earlier one. No synthetic or assumed values are ever written.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from newsforge.db.models import analytics


def _fmt_ts(value: Optional[str]) -> str:
    """Normalize an ISO string / datetime to second-precision ISO."""
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


def record_slot_rendered(session, *, article_id: str, slot_key: str,
                         placement: str, provider: str,
                         recorded_at: Optional[str] = None) -> dict:
    """Record that an ad slot was rendered into an article page (server-side).

    This is the ONLY event that can be recorded server-side during SSR.
    All subsequent events (request, loaded, impression, click, revenue)
    happen client-side or via provider webhooks."""
    ts_ = _fmt_ts(recorded_at)
    dim = f"slot_rendered:{slot_key}"
    if _exists(session, entity_id=article_id, metric="ad_event",
               dimension_value=dim, recorded_at=ts_):
        return {"entity_id": article_id, "recorded_at": ts_, "rows_created": 0}
    session.add(analytics(entity_id=str(article_id), metric="ad_event",
                          dimension_value=dim, value=1.0, recorded_at=ts_))
    session.flush()
    return {"entity_id": article_id, "recorded_at": ts_, "rows_created": 1}


def record_ad_request(session, *, article_id: str, slot_key: str,
                      provider: str, recorded_at: Optional[str] = None) -> dict:
    """Record that an ad request was sent to the provider (client-side).

    Only recorded when client-side JS fires the request event."""
    ts_ = _fmt_ts(recorded_at)
    dim = f"ad_request:{slot_key}"
    if _exists(session, entity_id=article_id, metric="ad_event",
               dimension_value=dim, recorded_at=ts_):
        return {"entity_id": article_id, "recorded_at": ts_, "rows_created": 0}
    session.add(analytics(entity_id=str(article_id), metric="ad_event",
                          dimension_value=dim, value=1.0, recorded_at=ts_))
    session.flush()
    return {"entity_id": article_id, "recorded_at": ts_, "rows_created": 1}


def record_ad_impression(session, *, article_id: str, slot_key: str,
                         provider: str, recorded_at: Optional[str] = None) -> dict:
    """Record a verified ad impression (viewability confirmed).

    ONLY recorded when the provider reports a real impression.
    Never assumed from slot_rendered or ad_request."""
    ts_ = _fmt_ts(recorded_at)
    dim = f"ad_impression:{slot_key}"
    if _exists(session, entity_id=article_id, metric="ad_event",
               dimension_value=dim, recorded_at=ts_):
        return {"entity_id": article_id, "recorded_at": ts_, "rows_created": 0}
    session.add(analytics(entity_id=str(article_id), metric="ad_event",
                          dimension_value=dim, value=1.0, recorded_at=ts_))
    session.flush()
    return {"entity_id": article_id, "recorded_at": ts_, "rows_created": 1}


def record_ad_click(session, *, article_id: str, slot_key: str,
                    provider: str, recorded_at: Optional[str] = None) -> dict:
    """Record a verified ad click event.

    ONLY recorded when the provider reports a real click.
    Never assumed or synthetic."""
    ts_ = _fmt_ts(recorded_at)
    dim = f"ad_click:{slot_key}"
    if _exists(session, entity_id=article_id, metric="ad_event",
               dimension_value=dim, recorded_at=ts_):
        return {"entity_id": article_id, "recorded_at": ts_, "rows_created": 0}
    session.add(analytics(entity_id=str(article_id), metric="ad_event",
                          dimension_value=dim, value=1.0, recorded_at=ts_))
    session.flush()
    return {"entity_id": article_id, "recorded_at": ts_, "rows_created": 1}


def record_ad_revenue(session, *, article_id: str, slot_key: str,
                      provider: str, amount: float, currency: str = "USD",
                      recorded_at: Optional[str] = None) -> dict:
    """Record real ad revenue from a provider report.

    ONLY recorded when the provider delivers actual revenue data.
    Never fabricated or estimated."""
    if amount < 0:
        raise ValueError("revenue amount must be >= 0")
    ts_ = _fmt_ts(recorded_at)
    dim = f"ad_revenue:{slot_key}:{currency}"
    if _exists(session, entity_id=article_id, metric="ad_event",
               dimension_value=dim, recorded_at=ts_):
        return {"entity_id": article_id, "recorded_at": ts_, "rows_created": 0}
    session.add(analytics(entity_id=str(article_id), metric="ad_event",
                          dimension_value=dim, value=float(amount), recorded_at=ts_))
    session.flush()
    return {"entity_id": article_id, "recorded_at": ts_, "rows_created": 1}


def get_ad_metrics(session, *, article_id: Optional[str] = None,
                   since: Optional[str] = None) -> dict:
    """Aggregate ad metrics from the analytics table.

    Returns counts per event type. Only includes real recorded events —
    never fabricates missing data."""
    q = session.query(analytics).filter(analytics.metric == "ad_event")
    if article_id:
        q = q.filter(analytics.entity_id == article_id)
    if since:
        q = q.filter(analytics.recorded_at >= since)

    metrics = {
        "slot_rendered": 0,
        "ad_request": 0,
        "ad_impression": 0,
        "ad_click": 0,
        "ad_revenue_count": 0,
        "ad_revenue_total": 0.0,
    }
    for row in q.all():
        dim = row.dimension_value or ""
        if dim.startswith("slot_rendered:"):
            metrics["slot_rendered"] += int(row.value or 0)
        elif dim.startswith("ad_request:"):
            metrics["ad_request"] += int(row.value or 0)
        elif dim.startswith("ad_impression:"):
            metrics["ad_impression"] += int(row.value or 0)
        elif dim.startswith("ad_click:"):
            metrics["ad_click"] += int(row.value or 0)
        elif dim.startswith("ad_revenue:"):
            metrics["ad_revenue_count"] += 1
            metrics["ad_revenue_total"] += float(row.value or 0)
    return metrics
