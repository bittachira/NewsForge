"""Trust Engine gate tests (section 6, 7).

Prove the contextual trust score is explainable (structured factors), that a high-trust story does
NOT blindly lift a new low-trust claim (adversarial Case 2), that duplicate sourcing is not counted
as independent corroboration (Case 1), and that scoring is fully deterministic. No LLM decides
trust.
"""
from __future__ import annotations

import pytest

from newsforge.verify.trust import evaluate_freshness, evaluate_trust
from newsforge.verify.corroboration import independent_sources_from


def test_high_trust_source_scores_higher_than_low():
    high, _ = evaluate_trust(source_tiers=["TIER_1"], evidence_source_ids=["a"])
    low, _ = evaluate_trust(source_tiers=["TIER_4"], evidence_source_ids=["a"])
    assert high > low


def test_numeric_and_string_tiers_are_equivalent():
    as_strings, _ = evaluate_trust(source_tiers=["TIER_2", "TIER_1"], evidence_source_ids=["a"])
    as_numbers, _ = evaluate_trust(source_tiers=[80.0, 95.0], evidence_source_ids=["a"])
    assert as_strings == as_numbers


def test_no_source_data_is_neutral_not_high():
    score, factors = evaluate_trust(evidence_source_ids=["a"])
    assert score < 60
    assert "no_source_trust_data" in factors["negative"]


# --------------------------------------------------------------------------- #
# Corroboration: DISTINCT sources only (section 3) -- adversarial Case 1
# --------------------------------------------------------------------------- #
def test_duplicate_sources_are_not_independent():
    """Two articles repeating the same source count as ONE independent source."""
    assert independent_sources_from(["s1", "s1"]) == 1
    assert independent_sources_from(["s1", "s2"]) == 2


def test_duplicate_sourcing_lower_trust_than_two_distinct_sources():
    dup, _ = evaluate_trust(source_tiers=["TIER_2", "TIER_2"], evidence_source_ids=["s1", "s1"])
    distinct, _ = evaluate_trust(source_tiers=["TIER_2", "TIER_2"], evidence_source_ids=["s1", "s2"])
    assert dup < distinct


def test_two_distinct_sources_are_independently_corroborated():
    score, factors = evaluate_trust(source_tiers=["TIER_2", "TIER_2"], evidence_source_ids=["s1", "s2"])
    assert "independently-corroborated" in factors["positive"]


# --------------------------------------------------------------------------- #
# Adversarial Case 2: a high-trust story must NOT lift a new low-trust claim
# --------------------------------------------------------------------------- #
def test_new_low_trust_claim_does_not_inherit_high_story_trust():
    """A historically reliable story receiving a TIER_4 rumor does not inherit the old trust."""
    score, factors = evaluate_trust(
        source_tiers=["TIER_4"], evidence_source_ids=["rumor-item"], freshness_scores=[100],
    )
    assert score < 60
    assert "single-source-only" in factors["negative"]


# --------------------------------------------------------------------------- #
# Contradictions (section 5) -- contradictory evidence lowers trust and is explained
# --------------------------------------------------------------------------- #
def test_contradictory_evidence_lowers_trust_and_is_explained():
    clean, _ = evaluate_trust(source_tiers=["TIER_2"], evidence_source_ids=["a", "b"])
    contradicted, factors = evaluate_trust(
        source_tiers=["TIER_2"], evidence_source_ids=["a", "b"], contradiction_count=1,
    )
    assert contradicted < clean
    assert any("contradiction" in n for n in factors["negative"])


# --------------------------------------------------------------------------- #
# Freshness (section 4) -- time-injectable so tests never depend on the current time
# --------------------------------------------------------------------------- #
def test_fresh_evidence_scores_full():
    score, stale = evaluate_freshness(publication_dates=["2026-09-05T10:00:00+00:00"],
                                      info_type="news", reference_time="2026-09-06T10:00:00+00:00")
    assert score == 100 and stale == [False]


def test_stale_evidence_is_flagged():
    # News window is ~1 day; a claim from months ago is stale.
    score, stale = evaluate_freshness(publication_dates=["2026-01-01T00:00:00+00:00"],
                                      info_type="news", reference_time="2026-09-05T00:00:00+00:00")
    assert score < 100 and stale == [True]


# --------------------------------------------------------------------------- #
# Determinism -- same inputs, identical outputs across repeated runs (section 15)
# --------------------------------------------------------------------------- #
def test_trust_scoring_is_deterministic():
    inputs = dict(source_tiers=["TIER_1", "TIER_2"], evidence_source_ids=["a", "b", "c"],
                  contradiction_count=0, risk_level="GREEN", freshness_scores=[90, 85])
    a, fa = evaluate_trust(**inputs)
    b, fb = evaluate_trust(**inputs)
    assert (a, fa) == (b, fb)


def test_risk_deduction_is_applied_to_trust():
    green, _ = evaluate_trust(source_tiers=["TIER_1"], evidence_source_ids=["a", "b"], risk_level="GREEN")
    red, factors = evaluate_trust(source_tiers=["TIER_1"], evidence_source_ids=["a", "b"], risk_level="RED")
    assert red < green
    assert factors["risk_deduction"] > 0
