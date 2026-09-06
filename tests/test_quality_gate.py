"""Quality Gate gate tests (section 9).

Prove the pre-publish gate blocks content that fails factual integrity, contains contradictions, or
carries RED-risk allegations -- and lets clean, well-sourced claims through. Freshness is a soft
signal: it lowers the composite score but does not by itself block publishing (section 6 owns it).
"""
from __future__ import annotations

import pytest

from newsforge.verify.quality import evaluate_quality


def _supported(text="El impuesto es de 3 euros", source_item_ids=None, tiers=None, publication_date=None):
    return {"text": text, "source_item_ids": source_item_ids or [], "tiers": tiers or [],
            "publication_date": publication_date}


# --------------------------------------------------------------------------- #
# Factual integrity (section 9)
# --------------------------------------------------------------------------- #
def test_supported_claim_passes_factual_integrity():
    passed, score, reasons = evaluate_quality(claims=[_supported(source_item_ids=["s1", "s2"])])
    assert passed is True and "unsupported_claim" not in reasons["hard_failures"]


def test_unsupported_claim_among_many_blocks_publishing():
    """Case 5: one unsupported sentence among ten supported claims must be detected."""
    claims = [_supported(source_item_ids=["s1"])] * 9 + [_supported(text="claim sin fuente", source_item_ids=[])]
    passed, score, reasons = evaluate_quality(claims=claims)
    assert passed is False
    assert "unsupported_claim" in reasons["hard_failures"]


def test_fabricated_source_blocks_publishing():
    """A claim whose backing sources do not exist must be rejected (section 9 source integrity)."""
    claims = [_supported(source_item_ids=["s1"], tiers=["TIER_1"])]
    passed, score, reasons = evaluate_quality(claims=claims, all_sources_verified=False)
    assert passed is False
    assert "fabricated_source" in reasons["hard_failures"]


# --------------------------------------------------------------------------- #
# Contradictions (section 5) -- never hidden
# --------------------------------------------------------------------------- #
def test_fact_checked_false_is_a_hard_failure():
    claims = [{"text": "aprobo", "source_item_ids": ["s1"], "fact_result": "FALSE"},
              {"text": "rechazo", "source_item_ids": ["s2"], "fact_result": "TRUE"}]
    passed, score, reasons = evaluate_quality(claims=claims)
    assert passed is False
    assert "contradiction_detected" in reasons["hard_failures"]


# --------------------------------------------------------------------------- #
# Freshness (section 4) -- soft signal: lowers score, does not hard-block
# --------------------------------------------------------------------------- #
def test_stale_evidence_lowers_score_but_does_not_hard_block():
    fresh = evaluate_quality(claims=[_supported(source_item_ids=["s1"], publication_date="2026-09-06T10:00:00+00:00")],
                             reference_time="2026-09-07T10:00:00+00:00")
    stale = evaluate_quality(claims=[_supported(source_item_ids=["s1"], publication_date="2026-01-01T00:00:00+00:00")],
                             reference_time="2026-09-07T10:00:00+00:00")
    assert fresh[1] > stale[1]  # composite score drops when evidence goes stale


# --------------------------------------------------------------------------- #
# Clean content passes (section 9)
# --------------------------------------------------------------------------- #
def test_clean_well_sourced_claim_passes():
    claims = [_supported(source_item_ids=["a", "b"], tiers=["TIER_1", "TIER_2"],
                         publication_date="2026-09-06T10:00:00+00:00")]
    passed, score, reasons = evaluate_quality(claims=claims)
    assert passed is True and score >= 70


def test_red_risk_claim_is_flagged():
    claims = [{"text": "The minister is accused of embezzling public funds", "source_item_ids": ["s1"]}]
    passed, score, reasons = evaluate_quality(claims=claims)
    assert passed is False
    assert "high_risk" in reasons["hard_failures"]
