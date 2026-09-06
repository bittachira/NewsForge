"""NewsForge verification layer (Phase P3).

Deterministic, explainable and auditable trust / claim / quality-gate / decision machinery.
Every module is pure where possible so the same inputs always yield the same outputs (§15), and no
LLM acts as final authority for TRUST / RISK / QUALITY / DECISION (§16).

Modules:
- claims.py       -- Claim Engine + provenance reconstruction (section 1, 2)
- corroboration.py-- independent-source counting + contradiction detection (section 3, 5)
- freshness.py    -- time-injectable freshness scoring (section 4)
- risk.py         -- deterministic GREEN/YELLOW/ORANGE/RED classification (section 8)
- trust.py        -- explainable contextual trust score with factors (section 6, 7)
- quality.py      -- pre-publish quality gate (section 9)
- decide.py       -- content decision engine + human-loop mapping (section 10, 16)
"""
from __future__ import annotations

from newsforge.verify.claims import (
    aggregate_verification, build_claim, is_supported, normalize_claim_ids, provenance_chain,
)
from newsforge.verify.corroboration import (
    detect_polarity_conflict, independent_sources_from, axis_of, tokenize,
)
from newsforge.verify.freshness import freshness_score, freshness_windows
from newsforge.verify.risk import classify_risk, risk_deduction, RISK_RULES, TYPE_FLOOR
from newsforge.verify.trust import (
    evaluate_trust, evaluate_freshness, clamp,
)
from newsforge.verify.quality import evaluate_quality
from newsforge.verify.decide import decide, human_loop_verdict_for

__all__ = [
    "aggregate_verification", "build_claim", "is_supported", "normalize_claim_ids", "provenance_chain",
    "detect_polarity_conflict", "independent_sources_from", "axis_of", "tokenize",
    "freshness_score", "freshness_windows",
    "classify_risk", "risk_deduction", "RISK_RULES", "TYPE_FLOOR",
    "evaluate_trust", "evaluate_freshness", "clamp",
    "evaluate_quality",
    "decide", "human_loop_verdict_for",
]
