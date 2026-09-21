"""P6 — Analytics & BI (section 28, section 48).

Read-only over persisted state. Event ingestion appends to the existing
``analytics`` table; cost always comes from P4's real AI cost engine
(``ai_jobs``/``ai_runs``). Nothing here mutates editorial state.

Ad analytics (ad-specific lifecycle tracking):
  slot_rendered, ad_request, ad_impression, ad_click, ad_revenue
Each event is recorded independently. No event implies another.
"""
from .events import record_revenue_event, record_traffic_event
from .queries import (
    content_roi_query,
    cost_query,
    revenue_query,
    traffic_query,
    total_ai_cost,
)
from .ads import (
    record_ad_click,
    record_ad_impression,
    record_ad_revenue,
    record_ad_request,
    record_slot_rendered,
    get_ad_metrics,
)

__all__ = [
    "record_traffic_event",
    "record_revenue_event",
    "record_slot_rendered",
    "record_ad_request",
    "record_ad_impression",
    "record_ad_click",
    "record_ad_revenue",
    "get_ad_metrics",
    "traffic_query",
    "revenue_query",
    "cost_query",
    "total_ai_cost",
    "content_roi_query",
]
