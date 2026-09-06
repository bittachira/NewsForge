"""H4 — publisher guard contract regression test.

is_auto_publishable() is the ONLY place a future distributor may decide whether to publish.
It must consume the Decision Engine's persisted verdict and MUST NOT re-evaluate risk, trust,
quality, contradictions, evidence or claims (doing so would duplicate :func:`decide` and create a
second, untested safety layer).

Contract under test:

    decision == PUBLISH  AND  human_override is False

This drives the REAL guard against REAL ``decisions`` model rows (no parallel abstraction),
covering every verdict/state that must or must not auto-publish.
"""
from __future__ import annotations

from newsforge.db import decisions
from newsforge.verify.persist import is_auto_publishable


def _decision_row(decision: str, human_override: bool = False) -> decisions:
    """Build a real persisted decision record with the two fields the guard reads."""
    return decisions(
        target_type="STORY",
        target_id="story-1",
        decision=decision,
        human_override=human_override,
    )


def test_publisher_must_only_publish_on_engine_decided_PUBLISH():
    """H4: a future publisher may auto-publish ONLY on an engine-issued PUBLISH with no override.

    All five contract states are checked against the real guard (no re-derivation of risk/quality):
      PUBLISH + human_override=False -> True
      REVIEW  + human_override=False -> False
      WAIT    + human_override=False -> False
      REJECT  + human_override=False -> False
      PUBLISH + human_override=True  -> False
    """
    cases = [
        # decision, human_override, expected auto-publishable
        ("PUBLISH", False, True),
        ("REVIEW", False, False),
        ("WAIT", False, False),
        ("REJECT", False, False),
        ("PUBLISH", True, False),
    ]

    for decision, human_override, expected in cases:
        row = _decision_row(decision=decision, human_override=human_override)
        assert is_auto_publishable(row) is expected
