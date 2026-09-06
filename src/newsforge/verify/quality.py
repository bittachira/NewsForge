"""Quality Gate (section 9).

Pre-publish gate. Pipeline: GENERATED -> FACT_CHECK -> QUALITY_CHECK -> DECISION. The gate must
detect -- and therefore block auto-publishing of -- content that fails any hard check:

- Factual integrity -- every claim is backed by evidence; no invented sources/citations.
- Source integrity  -- provenance valid; timestamps available when appropriate (soft warning).
- Contradictions    -- conflicting evidence is never hidden.
- Risk              -- RED content must not auto-publish (enforced here too, redundantly).
- Editorial safety  -- allegations / unverified claims route to human review.

Freshness is reported and lowers the composite score but does NOT by itself block publishing: a
stale fact is not necessarily wrong, so it is surfaced for the Trust Engine rather than hard-blocked
here (section 6 owns freshness). The gate returns ``(passed, score, reasons)`` where ``reasons`` is
fully structured so failures can be filtered, reported and audited deterministically (section 12). A
high trust score does NOT rescue a failed gate; the two engines are independent safety layers. No LLM
decides pass/fail.
"""
from __future__ import annotations

import re

from newsforge.verify.corroboration import detect_polarity_conflict, tokenize
from newsforge.verify.risk import classify_risk


def _split_sentences(text: str) -> list[str]:
    pieces = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [p for p in pieces if p]


def _claim_bundle(c):
    """Normalize a claim record into the fields the gate needs."""
    source_item_ids = []
    # Accept the plural ``source_item_ids``/``evidence_source_ids`` and also the singular
    # ``source_item_id`` produced by build_claim, so the gate consumes claims from either shape.
    for sid in c.get("source_item_ids") or c.get("evidence_source_ids") or [c.get("source_item_id")]:
        if sid:
            source_item_ids.append(str(sid))
    tiers = []
    for t in (c.get("tiers") or c.get("source_tiers") or []):
        tiers.append(t)
    return {
        "text": (c.get("text") or "").strip(),
        "source_item_ids": source_item_ids,
        "tiers": tiers,
        "publication_date": c.get("publication_date"),
        "fact_result": str(c.get("fact_result") or c.get("verified_at_state") or None).upper(),
    }


def _is_supported(bundle) -> bool:
    return len(bundle["source_item_ids"]) >= 1


# Reason codes that HARD-BLOCK auto-publishing (force passed=False).
HARD_FAIL_REASONS = {"unsupported_claim", "fabricated_source", "contradiction_detected",
                     "high_risk", "requires_human_review"}

# Composite-score weights for the six quality dimensions (sum to 1.0).
_WEIGHTS = {
    "factual_integrity": 0.30,
    "source_integrity": 0.15,
    "contradictions": 0.25,
    "risk": 0.20,
    "freshness": 0.10,
}


def evaluate_quality(
    *,
    claims=None,
    article_text=None,
    info_type: str = "news",
    reference_time: str | None = None,
    all_sources_verified: bool = True,
    policy_version: str = "p3.v1",
) -> tuple[bool, int, dict]:
    """Run the pre-publish quality gate.

    ``claims`` is a list of claim records (see :func:`_claim_bundle`). Each may carry its own
    ``fact_result`` ("TRUE"/"FALSE"/"UNCLEAR"). Returns ``(passed, score_0_to_100, reasons)``.
    Pure and deterministic (section 15).
    """
    bundles = [_claim_bundle(c) for c in (claims or [])]
    hard_failures: set[str] = set()
    checks: dict[str, dict] = {}

    # --- Factual integrity ------------------------------------------------------- #
    unsupported = [i for i, b in enumerate(bundles) if not _is_supported(b)]
    fabricated = [] if all_sources_verified else ["fabricated_source"]
    fi_failures = sorted(set(["unsupported_claim"] if unsupported else []) | set(fabricated))
    checks["factual_integrity"] = {
        "passed": not fi_failures,
        "failures": fi_failures,
        "details": {"unsupported_claims": unsupported, "fabricated_sources": fabricated},
    }
    hard_failures |= set(fi_failures)

    # --- Source integrity (timestamp availability -- soft warning) --------------- #
    missing_ts = [i for i, b in enumerate(bundles) if not b["publication_date"]]
    checks["source_integrity"] = {
        "passed": not missing_ts and all_sources_verified,
        "failures": sorted(set(missing_ts)) + (["missing_publication_timestamp"] if missing_ts else []),
    }

    # --- Contradictions ---------------------------------------------------------- #
    contradictions = {i for i, b in enumerate(bundles) if str(b["fact_result"]).upper() == "FALSE"}
    structural = detect_polarity_conflict(bundles)
    con_failures = sorted(set(list(contradictions) + [i for i, j in structural]))
    checks["contradictions"] = {
        "passed": not con_failures,
        "failures": con_failures,
        "details": {"fact_checked_false": list(contradictions), "polarity_conflicts": structural},
    }
    if con_failures:
        hard_failures.add("contradiction_detected")

    # --- Risk (RED claims must never auto-publish) ------------------------------- #
    red_indices, editorial_topics = _check_risk(bundles, article_text or "")
    checks["risk"] = {
        "passed": not red_indices and not editorial_topics,
        "failures": sorted(set(red_indices)) + (["editorial_topic:" + t for t in editorial_topics] if editorial_topics else []),
        "details": {"claim_risk_levels": [_risk_for_bundle(b) for b in bundles], "editorial_topics": editorial_topics},
    }
    if red_indices:
        hard_failures.add("high_risk")
    if editorial_topics:
        hard_failures.add("requires_human_review")

    return _finalize(
        bundles, checks, hard_failures,
        policy_version=policy_version, info_type=info_type, reference_time=reference_time,
    )


def _risk_for_bundle(bundle):
    return classify_risk(bundle["text"], info_type="news")


def _check_risk(bundles, article_text, *, red_categories=None):
    """Return (RED claim indices, editorial-safety topics triggered by the article)."""
    from newsforge.config import DecisionConfig

    red_categories = red_categories or DecisionConfig.red_categories
    red_indices = [i for i, b in enumerate(bundles) if _risk_for_bundle(b) == "RED"]
    topic_hits = sorted({cat for cat in red_categories if any(w in tokenize(article_text) for w in cat.split())})
    return red_indices, topic_hits


def _finalize(bundles, checks, hard_failures, *, policy_version="p3.v1", info_type="news", reference_time=None):
    """Combine component scores into a composite and decide pass/fail."""
    passed = not (hard_failures & HARD_FAIL_REASONS)

    # Weighted composite of the six dimensions; each sub-score is 0-100.
    total_w = sum(_WEIGHTS.values())
    weighted = 0.0
    for name, weight in _WEIGHTS.items():
        if name == "freshness":
            mean_score, _stale = _check_freshness(bundles, info_type, reference_time)
            sub = mean_score
        else:
            sub = 100.0 if checks[name]["passed"] else 0.0
        weighted += (weight / total_w) * sub
    score = clamp(round(weighted))

    reasons = {
        "policy_version": policy_version,
        "passed": passed,
        "score": score,
        "checks": checks,
        "hard_failures": sorted({str(f) for f in hard_failures}),
    }
    return passed, score, reasons


def clamp(value: float, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, int(round(value))))


def _check_freshness(bundles, info_type: str, reference_time):
    """Return (mean_freshness_score_0_to_100, is_stale_flags_per_claim).

    Freshness lowers the composite score but does NOT hard-fail the gate: a stale fact is not
    necessarily wrong, so it is surfaced here and scored by the Trust Engine rather than blocking
    publishing outright (section 6 owns freshness scoring). Pure and time-injectable.
    """
    from newsforge.verify.trust import evaluate_freshness as _ef

    dates = [b["publication_date"] for b in bundles if b["publication_date"]]
    mean_score, stale_flags = _ef(publication_dates=dates, info_type=info_type, reference_time=reference_time)
    return mean_score, stale_flags
