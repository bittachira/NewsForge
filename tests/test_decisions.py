"""Content Decision Engine gate tests (sections 10, 16).

Prove the decision engine is a deterministic state machine whose safety rules are enforced in code:
RED risk and contradictions can NEVER auto-publish, unsupported claims cannot publish silently, and
only fully-verified content reaches PUBLISH. No LLM decides publish/reject/wait.
"""
from __future__ import annotations

import pytest

from newsforge.verify.decide import decide, human_loop_verdict_for


# --------------------------------------------------------------------------- #
# Happy path (section 10)
# --------------------------------------------------------------------------- #
def test_green_content_with_strong_evidence_publishes():
    decision, reasons, verdict = decide(trust_score=92, risk_level="GREEN", quality_passed=True,
                                        all_claims_supported=True)
    assert decision == "PUBLISH" and reasons == [] and verdict == "GREEN"


def test_existing_story_with_new_verified_evidence_updates_not_creates():
    decision, _, _ = decide(trust_score=90, risk_level="GREEN", quality_passed=True,
                            all_claims_supported=True, story_exists=True, has_new_verified_evidence=True)
    assert decision == "UPDATE"


# --------------------------------------------------------------------------- #
# Forced REVIEW (section 10)
# --------------------------------------------------------------------------- #
def test_low_trust_forces_review_even_when_quality_passes():
    decision, reasons, _ = decide(trust_score=55, risk_level="GREEN", quality_passed=True,
                                  all_claims_supported=True, min_trust_to_publish=60.0)
    assert decision == "REVIEW" and any(r.startswith("trust_below_threshold") for r in reasons)


def test_quality_failure_forces_review():
    decision, _, _ = decide(trust_score=95, risk_level="GREEN", quality_passed=False,
                            hard_failures=["quality_failed"])
    assert decision == "REVIEW"


# --------------------------------------------------------------------------- #
# Forced WAIT (section 10) -- conflicting evidence needs adjudication
# --------------------------------------------------------------------------- #
def test_contradiction_routes_to_human_review():
    # Section 10: a contradiction must never publish silently; it routes to human review.
    decision, reasons, _ = decide(trust_score=95, risk_level="GREEN", quality_passed=False,
                                  has_contradiction=True)
    assert decision == "REVIEW" and "contradiction_detected" in reasons


# --------------------------------------------------------------------------- #
# Forced REJECT (section 10) -- RED allegation with no evidence cannot publish
# --------------------------------------------------------------------------- #
def test_red_risk_with_unsupported_claim_is_rejected():
    """RED + unsupported claim -> REJECT."""
    decision, _, _ = decide(trust_score=99, risk_level="RED", quality_passed=False, all_claims_supported=False)
    assert decision == "REJECT"


# --------------------------------------------------------------------------- #
# Section 11 hard rules -- the non-negotiable safety guarantees
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("risk_level,all_claims_supported,has_contradiction", [
    ("RED", True, False),      # RED clean evidence -> WAIT (never publish)
    ("RED", True, True),       # RED + contradiction -> WAIT
    ("RED", False, False),     # RED + unsupported -> REJECT
    ("RED", False, True),      # RED + everything wrong -> REJECT
])
def test_red_risk_can_never_auto_publish(risk_level, all_claims_supported, has_contradiction):
    decision, _, _ = decide(trust_score=99, risk_level=risk_level, quality_passed=True,
                            all_claims_supported=all_claims_supported, has_contradiction=has_contradiction)
    assert decision != "PUBLISH", f"RED content must never auto-publish: {decision}"


def test_unsupported_claim_cannot_publish_silently():
    """Case 5 / section 11: a claim with no evidence cannot publish without human review."""
    decision, _, _ = decide(trust_score=90, risk_level="GREEN", quality_passed=False,
                            hard_failures=["unsupported_claim"], all_claims_supported=False)
    assert decision != "PUBLISH"


def test_contradiction_cannot_silently_publish():
    """Contradictions must never publish silently (section 5)."""
    decision, _, _ = decide(trust_score=98, risk_level="GREEN", quality_passed=False, has_contradiction=True)
    assert decision != "PUBLISH"


# --------------------------------------------------------------------------- #
# Determinism + human-loop mapping (sections 15, 13)
# --------------------------------------------------------------------------- #
def test_decision_is_deterministic():
    inputs = dict(trust_score=80, risk_level="ORANGE", quality_passed=True, all_claims_supported=True)
    a = decide(**inputs)
    b = decide(**inputs)
    assert a == b


def test_human_loop_verdict_mapping():
    assert human_loop_verdict_for("PUBLISH") == "GREEN"
    assert human_loop_verdict_for("UPDATE") == "GREEN"
    assert human_loop_verdict_for("REVIEW") == "YELLOW"
    assert human_loop_verdict_for("WAIT") == "YELLOW"
