"""P6 - Analytics queries (section 28, section 48)."""
from __future__ import annotations

from typing import Optional
from sqlalchemy.orm import Session

from newsforge.db.models import analytics, decisions, generated_artifacts, ai_jobs, ai_runs


def _period(ts: str) -> str:
    return ts[:10] if len(ts) >= 10 else ts


def traffic_query(session: Session, *, entity_id: Optional[str] = None, period: Optional[str] = None) -> list[dict]:
    rows = session.query(analytics).filter_by(metric="traffic").all()
    agg: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (str(row.entity_id or ""), _period(row.recorded_at))
        slot = agg.setdefault(key, {"entity_id": key[0], "period": key[1], "views": 0.0, "users": 0.0})
        slot["views"] += float(row.value or 0.0)
    return sorted(agg.values(), key=lambda r: (r["period"], r["entity_id"]))


def revenue_query(session: Session, *, entity_id: Optional[str] = None, period: Optional[str] = None, currency: Optional[str] = None) -> list[dict]:
    q = session.query(analytics).filter_by(metric="revenue")
    if entity_id is not None:
        q = q.filter_by(entity_id=str(entity_id))
    agg: dict[tuple[str, str, str], float] = {}
    for row in q.all():
        if not _match_period(period, row.recorded_at):
            continue
        cur = str(row.dimension_value or "USD")
        if currency is not None and cur != str(currency):
            continue
        key = (str(row.entity_id or ""), _period(row.recorded_at), cur)
        agg[key] = agg.get(key, 0.0) + float(row.value or 0.0)
    return [{"entity_id": k[0], "period": k[1], "currency": k[2], "revenue": v} for k, v in sorted(agg.items())]


def _match_period(period: Optional[str], ts: str) -> bool:
    if period is None:
        return True
    p = period[:10] if len(period) >= 10 else period
    t = ts[:10] if len(ts) >= 10 else ts
    return p == t


def _ai_cost_rows(session: Session) -> list[dict]:
    runs = session.query(ai_runs).all()
    jobs_by_id = {str(j.id): j for j in session.query(ai_jobs).all()}
    art_by_id = {str(a.artifact_id): a for a in session.query(generated_artifacts).all()}
    out = []
    for run in runs:
        job = jobs_by_id.get(str(run.job_id)) if run.job_id else None
        art = art_by_id.get(str(run.run_id))
        out.append({
            "content_id": str(run.run_id),
            "story_id": str(art.story_id) if art is not None else None,
            "period": _period(job.created_at if job is not None else run.created_at),
            "cost_usd": float(job.cost_usd or 0.0) if job is not None else 0.0,
            "tokens_input": int(job.tokens_input or 0) if job is not None else 0,
            "tokens_output": int(job.tokens_output or 0) if job is not None else 0,
        })
    return out


def cost_query(session: Session, *, content_id: Optional[str] = None, story_id: Optional[str] = None, period: Optional[str] = None) -> list[dict]:
    rows = _ai_cost_rows(session)
    if content_id is not None:
        rows = [r for r in rows if r["content_id"] == str(content_id)]
    if story_id is not None:
        rows = [r for r in rows if r["story_id"] == str(story_id)]
    if period is not None:
        rows = [r for r in rows if r["period"] == str(period)]
    return sorted(rows, key=lambda r: (r["period"], r["content_id"]))


def total_ai_cost(session: Session, *, period: Optional[str] = None) -> float:
    q = session.query(ai_jobs)
    total = 0.0
    for job in q.all():
        if period is not None and _period(job.created_at) != str(period):
            continue
        total += float(job.cost_usd or 0.0)
    return total


def content_roi_query(session: Session, *, entity_id: Optional[str] = None, period: Optional[str] = None) -> list[dict]:
    """Deterministic content ROI per (entity, currency).

    MULTI-CURRENCY SEMANTIC CORRECTION:
    * Cost is always in USD. Revenue may be in multiple currencies.
    * ROI only calculated when revenue and cost are in the SAME currency.
    * EUR revenue vs USD cost -> roi=None with status="MULTI_CURRENCY_UNRESOLVED"
    
    Costs are matched by story_id from generated_artifacts (the AI run's artifact).
    Revenue is keyed by (entity_id, currency) from analytics table.
    """
    # Aggregate traffic by entity/period
    if entity_id is not None:
        traffic = {entity_id: {"views": 0.0, "users": 0.0}}
    else:
        traffic = {}
    
    for r in traffic_query(session, entity_id=entity_id, period=period):
        slot = traffic.setdefault(r["entity_id"], {"views": 0.0, "users": 0.0})
        slot["views"] += float(r["views"])
        slot["users"] += float(r["users"])
    
    # Aggregate revenue by (entity_id, currency)
    if entity_id is not None:
        revenue = {}
    else:
        revenue = {}
    
    for r in revenue_query(session, entity_id=entity_id, period=period):
        key = (r["entity_id"], r["currency"])
        revenue[key] = revenue.get(key, 0.0) + float(r["revenue"])
    
    # Get costs: ALWAYS use story_id filter to match AI runs/artifacts
    # Costs are attached to generated artifacts via story_id from generated_artifact table
    cost_by_story: dict[str, float] = {}
    for r in cost_query(session, story_id=entity_id if entity_id else None):
        story_id_str = str(r.get("story_id", "")) if r.get("story_id") else None
        if story_id_str:
            cost_by_story[story_id_str] = cost_by_story.get(story_id_str, 0.0) + r["cost_usd"]
    
    out: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    
    if entity_id is not None:
        all_entities = {entity_id}
        # Build currency set from revenue keys when entity_id matches the revenue key
        all_currencies = {key[1] for key in revenue.keys()} if revenue else set()
    else:
        all_entities = sorted(set(traffic.keys()) | {k[0] for k in revenue.keys()})
        # Extract currencies from the keys: key is (entity_id, currency)
        all_currencies = {k[1] for k in revenue.keys()} if revenue else set()
    
    for ent in all_entities:
        for cur in all_currencies:
            key = (ent, cur)
            if key not in seen_keys:
                seen_keys.add(key)
                
                t = traffic.get(ent, {"views": 0.0, "users": 0.0})
                # Get revenue for this currency using the proper key
                rev_cur = float(revenue.get((ent, cur), 0.0))
                # Get cost for this entity (from story_id matching in generated_artifact)
                cost = float(cost_by_story.get(ent, 0.0))
                
                if cost > 0:
                    if cur == "USD":
                        roi: Optional[float] = (rev_cur - cost) / cost
                        status = "OK"
                    else:
                        roi = None
                        status = "MULTI_CURRENCY_UNRESOLVED"
                else:
                    roi = None
                    status = "NO_COST_BASELINE"
                
                out.append({"entity_id": ent, "currency": cur, "views": t["views"],
                            "users": t["users"], "revenue": rev_cur,
                            "cost_usd": cost, "roi": roi, "roi_status": status})
    
    return sorted(out, key=lambda r: (r["entity_id"], r["currency"]))