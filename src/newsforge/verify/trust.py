"""Trust Engine (section 6, 7).

Contextual, explainable trust scoring. Unlike P2's story-level proxy (mean of source tiers),
this engine evaluates a claim/story against its CURRENT evidence and records exactly what drove
the number so an auditor can see the reasoning:

    Trust Score = clamp(0.40*source_trust + 0.25*corroboration + 0.25*freshness)
                  - contradiction_penalty - risk_deduction

Every component is deterministic and pure; factors_json records positive/negative reasons.
A high-trust story does NOT automatically lift a new low-trust claim -- the score is computed
from the evidence backing THIS information, never inherited blindly (adversarial Case 2). No LLM
decides trust.
"""
from __future__ import annotations

from datetime import datetime, timezone

from newsforge.verify.corroboration import independent_sources_from
from newsforge.verify.risk import RISK_DEDUCTION


def clamp(value: float, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, int(round(value))))


def _mean(values):
    values = [v for v in (values or []) if v is not None]
    return sum(values) / len(values) if values else None


def _tier_to_number(value):
    """Coerce a source trust value to a 0-100 number.

    Accepts either a numeric score (int/float) or a SourceTier string such as ``TIER_2``,
    which is mapped through the tier baseline. This keeps :func:`evaluate_trust` usable from
    anywhere -- pure unit tests pass numbers, while DB-backed callers pass tier strings.
    """
    if isinstance(value, str):
        value = value.strip().upper()
        if value.startswith("TIER_"):
            return float(_tier_baseline(value))
    try:
        return float(value)
    except (TypeError, ValueError):
        return 50.0


def _tier_baseline(tier: str) -> int:
    from newsforge.sources.trust import TIER_BASE_TRUST
    return TIER_BASE_TRUST.get(tier.upper(), 50)


def evaluate_trust(
    *,
    source_tiers=None,
    evidence_source_ids=None,
    contradiction_count: int = 0,
    risk_level: str | None = "GREEN",
    freshness_scores=None,
    policy_version: str = "p3.v1",
) -> tuple[int, dict]:
    """Compute an explainable trust score (0-100) for a claim or story.

    source_tiers -- tiers of the sources backing this information (e.g. ["TIER_2"]).
    evidence_source_ids -- source_item ids supporting it (drives corroboration).
    freshness_scores -- per-claim freshness scores, or a single int for one claim.
    Returns (trust_score, factors) where factors is the structured explanation.
    """
    positive: list[str] = []
    negative: list[str] = []

    # --- Source trust component (0-100): quality of backing sources --------------- #
    numeric_tiers = [_tier_to_number(t) for t in (source_tiers or [])]
    if numeric_tiers:
        mean_tier = clamp(_mean(numeric_tiers))
        source_component = mean_tier
        positive.append("source_trust=" + str(mean_tier))
        best = max(str(t).upper() for t in numeric_tiers)
        if best == "TIER_1":
            positive.append("primary-or-official-source")
    else:
        source_component = 50
        negative.append("no_source_trust_data")

    # --- Corroboration component (0-100): DISTINCT independent sources only -------- #
    if not evidence_source_ids:
        corroboration_component = 0
        negative.append("no_evidence")
    else:
        total_evidence = len(evidence_source_ids)
        independent = independent_sources_from(evidence_source_ids)
        if independent >= 2:
            # Genuinely corroborated by multiple DISTINCT sources (section 3).
            corroboration_component = clamp(100 * min(independent, 3) / 3.0)
            positive.append("independently-corroborated")
        else:
            # A single source is "supported" but not independently corroborated; it must be a
            # high-tier seed to publish on its own (section 7).
            corroboration_component = 50
            negative.append("single-source-only")

    # --- Freshness component (0-100) --------------------------------------------- #
    if freshness_scores is not None and any(s is not None for s in freshness_scores):
        fresh_mean = clamp(_mean(freshness_scores))
        stale_flags = [s for s in freshness_scores if isinstance(s, tuple) and s[1]]
        if stale_flags:
            negative.append("stale-evidence")
        else:
            positive.append("recently-verified")
    elif freshness_scores is not None:
        fresh_mean = clamp(_mean(freshness_scores))
    else:
        fresh_mean = 100
        negative.append("freshness_not_evaluated")
    freshness_component = fresh_mean

    # --- Contradiction penalty --------------------------------------------------- #
    contradiction_penalty = min(60, contradiction_count * 25)
    if contradiction_count > 0:
        negative.append("contradiction-detected-x" + str(contradiction_count))

    # --- Risk deduction (from risk engine) --------------------------------------- #
    risk_deduction = RISK_DEDUCTION.get(str(risk_level or "GREEN").upper(), 0)
    if risk_deduction:
        negative.append("risk-level=" + str(risk_level))

    raw = (0.40 * source_component + 0.25 * corroboration_component + 0.25 * freshness_component)
    score = clamp(raw - contradiction_penalty - risk_deduction)

    factors = {
        "policy_version": policy_version,
        "source_trust_component": source_component,
        "corroboration_component": corroboration_component,
        "freshness_component": freshness_component,
        "contradiction_penalty": contradiction_penalty,
        "risk_deduction": risk_deduction,
        "positive": positive,
        "negative": negative,
    }
    return score, factors


def evaluate_freshness(
    *,
    publication_dates=None,
    info_type: str = "news",
    reference_time: str | None = None,
) -> tuple[int, list[bool]]:
    """Score freshness across several claims at once.

    Returns (mean_score_0_to_100, is_stale_flags_per_claim). Each claim's staleness is scored
    independently so a single stale claim can be flagged without discarding the rest. Pure and
    time-injectable (section 4, 15).
    """
    # Use the injected reference time when provided so freshness is deterministic and
    # testable (section 4, 15); fall back to wall-clock only when none is supplied.
    now = _parse(reference_time) or _utcnow()
    windows = {
        "news": 1.0, "breaking": 1.0, "financial": 7.0, "health": 2.0,
        "policy": 30.0, "law": 90.0, "general": 90.0,
    }
    info_key = str(info_type).lower()
    window = windows.get(info_key, 30.0)

    scores, stale_flags = [], []
    for pub in (publication_dates or []):
        created_dt = _parse(pub) or now
        age_days = max(0.0, (now - created_dt).total_seconds() / 86400.0)
        if age_days <= window:
            s, stale = 100, False
        elif window <= 0 or info_key in {"evergreen", "reference"}:
            s, stale = max(60, 100 - min(age_days / 365.0 * 100.0, 40)), False
        else:
            decay = min(60.0, (age_days - window) / (window * 2.0) * 60.0)
            s, stale = max(40, 100 - decay), True
        scores.append(s)
        stale_flags.append(stale)
    return clamp(_mean(scores)) if scores else 0, stale_flags


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
