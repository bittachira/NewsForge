"""CONTENT DECISION ENGINE (section 10, 16).

The decision engine is the final gate before anything can auto-publish. It consumes the outputs of
the Trust Engine and Quality Gate and emits a deterministic state-machine decision:

    PUBLISH | REVIEW | WAIT | REJECT | UPDATE

Its single most important job is to make it *impossible* for unsafe content to auto-publish
(section 11). The precedence below is ordered so that safety checks always win over convenience:

    1. RED risk ---------------------------------------------> never silent publish (WAIT/REJECT)
    2. Quality-gate hard failure ----------------------------> REVIEW / REJECT
    3. Contradiction detected -------------------------------> REVIEW (human must adjudicate)
    4. Trust below threshold --------------------------------> REVIEW
    5. Claims present but no supporting evidence ------------> WAIT (unverified, cannot publish)
    6. Everything passes ------------------------------------> PUBLISH (or UPDATE existing story)

Every decision is deterministic: the same inputs always yield the same decision and reasons (§15).
Reasons are structured codes (not free text) so they can be filtered, reported and audited
(section 12). No LLM decides publish/reject/wait.
"""
from __future__ import annotations

from newsforge.config import TrustConfig
from newsforge.db.models import DecisionState, HumanLoopVerdict


# HumanLoopVerdict mapping for the review queue integration (§13, §18).
def human_loop_verdict_for(decision: str) -> str:
    """Map a decision to how urgently it needs a human. GREEN = auto-publishable."""
    if decision in (DecisionState.PUBLISH.value, DecisionState.UPDATE.value):
        return HumanLoopVerdict.GREEN.value
    return HumanLoopVerdict.YELLOW.value  # REVIEW / WAIT both need human attention


def decide(
    *,
    trust_score: float = 0.0,
    risk_level: str = "GREEN",
    quality_passed: bool = True,
    hard_failures=None,
    all_claims_supported: bool = True,
    has_contradiction: bool = False,
    story_exists: bool = False,
    has_new_verified_evidence: bool = False,
    min_trust_to_publish: float = 60.0,
    policy_version: str = "p3.v1",
) -> tuple[str, list[str], str]:
    """Return ``(decision, reasons, human_loop_verdict)`` for an evaluated piece of content.

    All inputs are deterministic; ``reasons`` is a list of structured reason codes (§12). The
    function never returns PUBLISH when risk is RED, a contradiction exists, or claims lack
    critical evidence (section 11 hard rules).
    """
    hard_failures = set(hard_failures or [])
    reasons: list[str] = []

    # --- 1. RED risk can NEVER auto-publish -------------------------------------- #
    if risk_level == "RED":
        if not all_claims_supported:
            return DecisionState.REJECT.value, ["red_risk", "unsupported_claim"], HumanLoopVerdict.RED.value
        if has_contradiction:
            return DecisionState.WAIT.value, ["red_risk", "conflicting_evidence"], HumanLoopVerdict.YELLOW
        # Clean evidence but RED topic still requires a human to adjudicate.
        return DecisionState.WAIT.value, ["red_risk_human_review_required"], HumanLoopVerdict.RED.value

    # --- 2. Quality-gate hard failures block auto-publish ------------------------ #
    if not quality_passed or ("contradiction_detected" in hard_failures):
        if has_contradiction:
            return DecisionState.REVIEW.value, ["quality_failed", "contradiction_detected"], HumanLoopVerdict.YELLOW
        if not all_claims_supported:
            # Non-RED unsupported/incomplete evidence -> REVIEW so a human can verify against
            # better sources; only RED+unsupported is auto-REJECTED (section 10).
            return DecisionState.REVIEW.value, ["incomplete_evidence"], HumanLoopVerdict.YELLOW
        return DecisionState.REVIEW.value, ["quality_failed"], HumanLoopVerdict.YELLOW

    # --- 3. Trust threshold ------------------------------------------------------ #
    if trust_score < min_trust_to_publish:
        reasons.append("trust_below_threshold")
        reasons.append(str(int(trust_score)))
        return DecisionState.REVIEW.value, reasons, HumanLoopVerdict.YELLOW

    # --- 4. Critical-evidence requirement ---------------------------------------- #
    if not all_claims_supported:
        return DecisionState.WAIT.value, ["unverified_claim"], HumanLoopVerdict.YELLOW

    # --- 5. Everything passes ---------------------------------------------------- #
    if story_exists and has_new_verified_evidence:
        reasons.append("new_verified_evidence")
        reasons.append("update_existing_story")
        return DecisionState.UPDATE.value, reasons, HumanLoopVerdict.GREEN
    return DecisionState.PUBLISH.value, reasons, HumanLoopVerdict.GREEN
