"""P6 — Analytics & BI (section 28, section 48).

Read-only over persisted state. Event ingestion appends to the existing
``analytics`` table; cost always comes from P4's real AI cost engine
(``ai_jobs``/``ai_runs``). Nothing here mutates editorial state.
"""
from .events import record_revenue_event, record_traffic_event
from .queries import (
    content_roi_query,
    cost_query,
    revenue_query,
    traffic_query,
    total_ai_cost,
)

__all__ = [
    "record_traffic_event",
    "record_revenue_event",
    "traffic_query",
    "revenue_query",
    "cost_query",
    "total_ai_cost",
    "content_roi_query",
]
