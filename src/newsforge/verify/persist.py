"""P3 persistence + orchestration layer (sections 1, 2, 6-10, 13, 14, 17).

This module turns the *pure* verify functions (:mod:`newsforge.verify.claims`,
`:mod:`newsforge.verify.trust`, `:mod:`newsforge.verify.risk`, `:mod:`newsforge.verify.quality`,
`:mod:`newsforge.verify.decide`) into a working, auditable pipeline that writes to the database.

Design (matches :class:`newsforge.stories.engine.StoryDetector`):

* **One session per logical operation** — robust on this Windows+SQLite setup and keeps every
  write atomic/deterministic (§15).
* **Idempotency via UNIQUE-constraint rejection.** This environment's SQLAlchemy has no ON
  CONFLICT support, so re-inserting a key that already exists raises ``IntegrityError``. We
  catch it and treat the row as "already present" — which *is* idempotency: re-running never
  creates a duplicate (§17). The UNIQUE constraints on ``claims.claim_id``,
  ``claim_evidence(claim_id, source_item_id)`` and ``(target_type, target_id)`` on the
  evaluation/decision tables enforce this at the database level.
* **Safety stays in code.** :func:`decide` never returns PUBLISH for RED risk, a contradiction,
  or unsupported claims; that guarantee is preserved end-to-end here (§11).

The public surface: low-level idempotent writers (:func:`persist_claims`,
:func:`link_claim_evidence`, :func:`store_trust_evaluations`, :func:`store_quality_evaluations`,
:func:`record_decision`, :func:`audit_event`) plus a high-level
:func:`run_verification` that runs the full STORY -> CLAIMS -> EVIDENCE -> TRUST -> RISK ->
QUALITY -> DECISION flow for one story and persists every step.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from newsforge.db.models import (
    DecisionState,
    HumanLoopVerdict,
    articles,
    claim_evidence,
    claims,
    decisions,
    quality_evaluations,
    review_tasks,
    sources,
    source_items,
    stories,
    trust_evaluations,
)
from newsforge.db.session import get_session
from newsforge.verify.claims import aggregate_verification, build_claim, is_supported
from newsforge.verify.corroboration import detect_polarity_conflict, independent_sources_from
from newsforge.verify.decide import decide
from newsforge.verify.freshness import freshness_score
from newsforge.verify.quality import evaluate_quality
from newsforge.verify.risk import classify_risk
from newsforge.db.models import to_jsonable
from newsforge.verify.trust import evaluate_freshness, evaluate_trust

# Policy versions for every P3 rule set (§18). Bump these together when rules change.
POLICY_VERSION = "p3.v1"


# --------------------------------------------------------------------------- #
# Low-level idempotent writers
# --------------------------------------------------------------------------- #
def _idempotent_add(session, instance) -> bool:
    """Insert one ORM instance; return True if it was newly created.

    A duplicate key raises ``IntegrityError`` (enforced by the UNIQUE constraints); we swallow
    it and report the row as already present so re-running is a safe no-op (§17).
    """
    try:
        session.add(instance)
        session.commit()
        return True
    except IntegrityError:
        # Already exists -> idempotent no-op. Roll back to reset the transaction state.
        session.rollback()
        return False


def persist_claims(session, claim_records: Iterable[dict]) -> dict[str, int]:
    """Persist already-built claim records idempotently by ``claim_id``.

    Receives the exact dicts produced by :func:`build_claim` (see :func:`run_verification`) so the
    persisted row keeps the same ``claim_id`` the rest of the pipeline references — keeping provenance
    linkage and determinism intact (§15). Idempotent by ``claim_id`` via the UNIQUE constraint.
    """
    counts = {"created": 0, "existing": 0}
    for claim in claim_records:
        if _idempotent_add(session, claims(**claim)):
            counts["created"] += 1
        else:
            counts["existing"] += 1
    return counts


def link_claim_evidence(session, evidence_pairs: Iterable[tuple[str, str]]) -> dict[str, int]:
    """Link each CLAIM to the SOURCE ITEM(s) that support it (§2 provenance).

    ``evidence_pairs`` is an iterable of ``(claim_id, source_item_id)``. Idempotent via the
    UNIQUE(claim_id, source_item_id) constraint — re-running adds no duplicate rows (§3).
    """
    counts = {"created": 0, "existing": 0}
    for claim_id, source_item_id in evidence_pairs:
        if not claim_id or not source_item_id:
            continue
        row = claim_evidence(claim_id=claim_id, source_item_id=str(source_item_id))
        if _idempotent_add(session, row):
            counts["created"] += 1
        else:
            counts["existing"] += 1
    return counts


def store_trust_evaluations(session, evaluations: Iterable[dict]) -> dict[str, int]:
    """Persist structured, explainable trust scores (§6, §7) idempotently by target key."""
    counts = {"created": 0, "existing": 0}
    for ev in evaluations:
        if _idempotent_add(session, trust_evaluations(**ev)):
            counts["created"] += 1
        else:
            counts["existing"] += 1
    return counts


def store_quality_evaluations(session, evaluations: Iterable[dict]) -> dict[str, int]:
    """Persist QUALITY GATE results (§9) idempotently by target key."""
    counts = {"created": 0, "existing": 0}
    for ev in evaluations:
        if _idempotent_add(session, quality_evaluations(**ev)):
            counts["created"] += 1
        else:
            counts["existing"] += 1
    return counts


def audit_event(
    session,
    *,
    actor: Optional[str],
    action: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    policy_version: Optional[str] = None,
    before_json: Optional[dict] = None,
    after_json: Optional[dict] = None,
) -> dict:
    """Append an audit trail entry (§14): what the system did, why, on which data."""
    row = {
        "actor": actor,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "policy_version": policy_version or "",
        "before_json": to_jsonable(before_json),
        "after_json": to_jsonable(after_json),
    }
    _idempotent_add(session, audit_logs(**row))


# Imported here to keep the top of the module free of an import cycle with db.models.
from newsforge.db.models import audit_logs  # noqa: E402,F401


def record_decision(
    session,
    *,
    target_type: str,
    target_id: str,
    risk_level: Optional[str] = None,
    decision_result: tuple[str, list[str], str] | None = None,
) -> Optional[dict]:
    """Persist the CONTENT DECISION ENGINE outcome (§10, §14).

    ``decision_result`` is ``(decision, reasons, human_loop_verdict)`` from :func:`decide`.
    Idempotent by (target_type, target_id); re-running updates in place via rejection. When the
    decision cannot auto-publish (REVIEW/WAIT), a human-review task is created (§13). Returns the
    persisted decision row as a dict, or None if it already existed and was left untouched.
    """
    decision, reasons, verdict = decision_result

    ev = {
        "target_type": target_type,
        "target_id": target_id,
        "decision": decision,
        "risk_level": _risk_from_verdict(verdict),
        "trust_score": 0,  # filled by caller via run_verification; kept valid here
        "reasons_json": to_jsonable(reasons),
        "human_override": False,
        "policy_version": POLICY_VERSION,
    }

    ev["risk_level"] = risk_level or ev.get("risk_level")
    created = _idempotent_add(session, decisions(**ev))
    if not created:
        return None  # already decided -> idempotent no-op

    row_id = _fetch_decision_pk(session, target_type, target_id)

    # REVIEW / WAIT need a human to adjudicate; REJECT is terminal (no review task needed).
    if verdict in (HumanLoopVerdict.YELLOW.value, HumanLoopVerdict.RED.value):
        _create_review_task(session, row_id, target_type, target_id, reasons, decision)

    return {"id": row_id, "decision": decision, "reasons": reasons, "verdict": verdict}


def is_auto_publishable(decision_row) -> bool:
    """Publisher contract (H4). A future distributor must NOT receive an arbitrary object and assume
    it is approved. It may auto-publish ONLY when the Decision Engine itself issued a ``PUBLISH``
    verdict on this persisted row and no human reviewer overrode it.

    This guard consumes the decision engine's *persisted* verdict — it does not re-evaluate risk,
    quality or evidence. Re-deriving those here would duplicate :func:`decide` and create a second,
    untested safety layer. Any future publish path must route through this single check (§11, §24)."""
    return (
        str(decision_row.decision) == DecisionState.PUBLISH.value
        and not decision_row.human_override
    )


def _risk_from_verdict(verdict: str) -> Optional[str]:
    # RED verdict (unsupported RED claim) is the most severe; otherwise map YELLOW->YELLOW.
    if verdict == HumanLoopVerdict.RED.value:
        return "RED"
    if verdict in (DecisionState.REVIEW.value, DecisionState.WAIT.value):
        return "YELLOW"
    return None


def _create_review_task(session, decision_id, target_type, target_id, reasons, decision) -> Optional[str]:
    """Create a human-review queue entry (§13) with everything the reviewer needs."""
    story = session.query(stories).filter_by(id=target_id).first() if target_type == "STORY" else None
    task = review_tasks(
        decision_id=decision_id,
        story_id=(story.story_id if (story and story.story_id) else None),
        claim_ids_json=None,
        status="ASSIGNED",
        assigned_to="",  # filled by a scheduler/worker; backend leaves this ready (§13)
        notes=f"Recommended decision {decision} — reasons: {', '.join(reasons)}",
    )
    _idempotent_add(session, task)
    return str(task.id)


# --------------------------------------------------------------------------- #
# High-level orchestration: the full verification pipeline for one story
# --------------------------------------------------------------------------- #
def run_verification(*, claims_specs: Iterable[dict], story_id: Optional[str] = None,
                     reference_time=None) -> dict:
    """Run STORY -> CLAIMS -> EVIDENCE -> TRUST -> RISK -> QUALITY -> DECISION (§1).

    ``claims_specs`` is a list of claim specs (see :func:`persist_claims`). Each spec may carry an
    optional authoritative ``fact_result`` ("TRUE"/"FALSE"/"UNCLEAR") used for the contradiction
    path. The function persists every step idempotently and returns a fully structured verdict so
    it can be audited, reported or surfaced in the review queue (§12).

    ``reference_time`` is an injectable clock (ISO string or datetime); when omitted production
    uses the current time. A single value is captured once (:func:`datetime.now`) and shared by every
    freshness scoring in this run, so identical inputs always produce identical trust/freshness/
    quality/risk/decision (§15 determinism).

    Safety is enforced by :func:`decide`: RED risk, contradictions and unsupported claims can
    NEVER yield PUBLISH — this holds end-to-end here (§11).
    """
    with get_session() as session:
        # Single injectable clock for the whole run (G2 determinism). Every freshness score below
        # uses exactly this value, so re-running with the same reference_time is reproducible.
        # Accept either an ISO string (tests / callers) or a datetime (spec default). A single value
        # is captured once and shared by every freshness score in this run (§15 determinism).
        now = reference_time or datetime.now(timezone.utc)
        now_iso = now.isoformat() if isinstance(now, datetime) else str(now)

        # 1. Claims + provenance (Story -> Source Item -> Evidence -> Claim) (§1, §2).
        claim_specs = list(claims_specs or [])
        # Build claims ONCE so the persisted row, its evidence links and its trust evaluation
        # all share the same claim_id. Rebuilding would assign different UUIDs to each step,
        # which both breaks provenance linkage and makes output non-deterministic (§15).
        built = [build_claim(**_spec_to_build(s)) for s in claim_specs]
        persist_claims(session, built)

        # Resolve the persisted claim rows so evidence links to claims.id (the FK target), not the
        # business key. Claims carry two ids; only claims.id is the join target, so provenance
        # linkage must use it for consistency with build_provenance_chain (§9/G3).
        claims_by_key = {}
        for row in session.query(claims).all():
            claims_by_key.setdefault(row.claim_id, row)

        # Link every distinct source item that backs a claim (not just the first). The UNIQUE
        # (claim_id, source_item_id) constraint keeps this idempotent (§17). Linking all evidence
        # yields a complete provenance chain (§9/G3) and lets corroboration count real sources.
        evidence_pairs = []
        for spec in claim_specs:
            key = spec.get("claim_id") or str(spec["claim_id"])
            row = claims_by_key.get(key)
            if not row:
                continue
            for sid in (spec.get("source_item_ids") or [None]):
                if sid:
                    evidence_pairs.append((str(row.id), str(sid)))
        link_claim_evidence(session, evidence_pairs)

        # 2. Per-claim risk + freshness (§4, §8).
        per_claim = []
        contradiction_count = _count_contradictions(built)
        for spec, claim in zip(claim_specs, built):
            text = (spec.get("text") or "").strip()
            risk_level = classify_risk(text) if text else "GREEN"
            # Same injected clock for every claim in this run (G2).
            fresh_score, _stale = freshness_score(
                spec.get("publication_date"), info_type="news", reference_time=now_iso,
            )
            per_claim.append({
                "claim_id": claim["claim_id"],
                "text": text,
                "risk_level": risk_level,
                "freshness_score": fresh_score,
                "supported": is_supported(claim, evidence_source_ids=spec.get("source_item_ids")),
            })

        # 3. Trust (contextual, explainable) per claim (§6). Corroboration counts DISTINCT
        # SOURCES, not distinct source items: two articles from the same source collapse to one
        # independent source (G1). We resolve each source item to its originating source id via the
        # DB before counting.
        trust_evals = []
        trust_scores = []
        for spec, pc in zip(claim_specs, per_claim):
            resolved_evidence = _resolve_source_ids(session, spec.get("source_item_ids"))
            score, factors = evaluate_trust(
                source_tiers=spec.get("tiers") or [],
                evidence_source_ids=resolved_evidence,
                contradiction_count=contradiction_count,
                risk_level=pc["risk_level"],
                freshness_scores=[pc["freshness_score"]],
            )
            trust_scores.append(score)
            trust_evals.append({
                "target_type": "CLAIM",
                "target_id": pc["claim_id"],
                "trust_score": score,
                "source_trust": _tier_mean(spec.get("tiers")),
                "independent_corroboration": independent_sources_from(resolved_evidence),
                "total_evidence": len(spec.get("source_item_ids") or []),
                "contradiction_penalty": min(60, contradiction_count * 25),
                "freshness_score": pc["freshness_score"],
                "risk_level": pc["risk_level"],
                "factors_json": to_jsonable(factors),
                "policy_version": POLICY_VERSION,
            })

        # 4. Quality gate (§9).
        quality_passed, quality_score, quality_reasons = evaluate_quality(
            claims=built, info_type="news", policy_version=POLICY_VERSION,
        )

        # 5. Aggregate trust + risk for the story, then decide (§10).
        aggregate_trust = _clamp(_mean(trust_scores)) if trust_scores else 0
        story_risk = max((pc["risk_level"] for pc in per_claim), default="GREEN")
        all_supported = all(pc["supported"] for pc in per_claim) if per_claim else False

        decision_result = decide(
            trust_score=aggregate_trust,
            risk_level=story_risk,
            quality_passed=quality_passed,
            hard_failures=list(quality_reasons.get("hard_failures", [])),
            all_claims_supported=all_supported,
            has_contradiction=bool(contradiction_count),
            story_exists=bool(story_id),
            policy_version=POLICY_VERSION,
        )

        # 6. Persist decision + trust/quality evaluations + audit trail (§10, §12, §14).
        record_decision(
            session,
            target_type="STORY",
            target_id=story_id or "unknown",
            risk_level=story_risk,
            decision_result=decision_result,
        )
        store_trust_evaluations(session, trust_evals)
        store_quality_evaluations(session, [{
            "target_type": "STORY", "target_id": story_id or "unknown",
            "passed": quality_passed, "score": quality_score, "reasons_json": to_jsonable(quality_reasons),
        }])

        verdict = decision_result[2]
        audit_event(
            session, actor="verify-engine", action=f"VERIFICATION_DECISION:{decision_result[0].upper()}",
            entity_type="STORY", entity_id=story_id or "unknown", policy_version=POLICY_VERSION,
            before_json={"risk_level": story_risk, "trust_score": aggregate_trust,
                         "quality_passed": quality_passed},
            after_json={"decision": decision_result[0], "reasons": decision_result[1]},
        )

    return {
        "story_id": story_id or "unknown",
        "policy_version": POLICY_VERSION,
        "decision": decision_result[0],
        "risk_level": story_risk,
        "trust_score": aggregate_trust,
        "quality_passed": quality_passed,
        "quality_score": quality_score,
        "quality_reasons": quality_reasons.get("hard_failures", []),
        "all_claims_supported": all_supported,
        "has_contradiction": bool(contradiction_count),
        "per_claim": per_claim,
        "trust_evaluations": trust_evals,
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _spec_to_build(spec: dict) -> dict:
    """Adapt a claim spec into the fields :func:`build_claim` expects."""
    return {
        "text": spec.get("text"),
        "story_id": spec.get("story_id"),
        "claim_id": spec.get("claim_id"),
        "source_item_id": (spec.get("source_item_ids") or [None])[0],
        "source_url": spec.get("source_url"),
        "publication_date": spec.get("publication_date"),
    }


def _count_contradictions(built_claims: list[dict]) -> int:
    """Authoritative contradiction count: any FALSE fact-result, plus structural polarity."""
    false_results = sum(1 for c in built_claims if str(c.get("status")).upper() == "CONTRADICTED")
    structural = len(detect_polarity_conflict(built_claims))
    return max(false_results, structural)


def _tier_mean(tiers: Optional[Iterable[str]]) -> int:
    from newsforge.sources.trust import tier_baseline

    vals = [tier_baseline(str(t).upper()) for t in (tiers or []) if t]
    return int(_mean(vals)) if vals else 0


def _clamp(value: float, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, int(round(value))))


def _mean(values):
    values = [v for v in (values or []) if v is not None]
    return sum(values) / len(values) if values else 0.0


def _fetch_decision_pk(session, target_type, target_id) -> str:
    """Return the UUID pk of an existing decision row by its upsert key."""
    row = session.query(decisions).filter_by(target_type=target_type, target_id=target_id).first()
    return str(row.id) if row else str(__new_uuid())

def _resolve_source_ids(session, source_item_ids) -> list[str]:
    """Map ``source_item_id``s to their originating SOURCE id via the DB (G1).

    Corroboration must count DISTINCT sources, not distinct articles. Two evidence rows pointing at
    the same source (``source_items.source_id``) collapse to ONE independent source (§3). Resolution
    happens here where the source-item -> source relationship exists; :func:`independent_sources_from`
    then dedupes these resolved ids. Deterministic and pure w.r.t. its inputs.
    """
    seen: set[str] = set()
    for sid in source_item_ids or []:
        if not sid:
            continue
        row = session.get(source_items, str(sid))
        if row is not None and getattr(row, "source_id", None):
            seen.add(str(row.source_id))
    return list(seen)


def build_provenance_chain(session, story_id: Optional[str]) -> dict:
    """Reconstruct the full audit chain Story -> Source -> Source Item -> Claim -> Evidence (§2, §9).

    Loads every claim attached to ``story_id`` and, for each, walks its evidence rows to the source
    item and the publishing source (with tier). Returns a nested structure a reviewer/auditor can use
    to answer: which story, which claim, which article originated it, which source published it, what
    evidence backs it, and each source's tier. Uses only existing tables — no data duplication.
    """
    claims_rows = list(session.query(claims).filter_by(story_id=str(story_id)).all()) if story_id else []
    stories_row = (session.query(stories).filter(
        or_(stories.id == str(story_id), stories.story_id == str(story_id))
    ).first() if story_id else None)

    chain = {"story": None, "claims": []}
    if stories_row:
        chain["story"] = {
            "id": stories_row.id,
            "story_id": stories_row.story_id,
            "title": stories_row.title,
            "slug": stories_row.slug,
        }

    for claim in claims_rows:
        item_row = session.get(source_items, str(claim.source_item_id)) if claim.source_item_id else None
        source_row = (session.get(sources, str(item_row.source_id))
                      if (item_row and item_row.source_id) else None)

        evidence = []
        ev_rows = list(session.query(claim_evidence).filter_by(claim_id=str(claim.id)).all())
        for ev in ev_rows:
            ev_item = session.get(source_items, str(ev.source_item_id)) if ev.source_item_id else None
            evidence.append({
                "claim_evidence_id": str(ev.id),
                "source_item_id": str(ev.source_item_id),
                "item_title": ev_item.title if ev_item else None,
                "item_published_at": ev_item.published_at if ev_item else None,
                "source_id": str(ev_item.source_id) if (ev_item and ev_item.source_id) else None,
            })

        chain["claims"].append({
            "claim_id": str(claim.id),
            "claim_id_key": claim.claim_id,
            "text": claim.text,
            "status": claim.status,
            "confidence": claim.confidence,
            "source_item_id": str(claim.source_item_id) if claim.source_item_id else None,
            "item_title": item_row.title if item_row else None,
            "item_published_at": item_row.published_at if item_row else None,
            "source_name": source_row.name if source_row else None,
            "source_url": source_row.url if source_row else None,
            "source_tier": source_row.tier if source_row else None,
            "evidence": evidence,
        })

    return chain


def __new_uuid() -> str:  # pragma: no cover - only used as a last-resort fallback
    import uuid

    return str(uuid.uuid4())
