"""Claim Engine gate tests (section 1, 2).

Prove that claims carry correct provenance, start UNVERIFIED with zero confidence, and that the
verification state transitions deterministically from authoritative fact-check results. No LLM is
involved in any of these decisions (section 16).
"""
from __future__ import annotations

import pytest

from newsforge.verify.claims import (
    aggregate_verification, build_claim, is_supported, normalize_claim_ids, provenance_chain,
)


# --------------------------------------------------------------------------- #
# Claims: construction + provenance (§2)
# --------------------------------------------------------------------------- #
def test_build_claim_starts_unverified_with_zero_confidence():
    claim = build_claim(text="El impuesto es de 3 euros", story_id="eu_small_package_tax_2026",
                        source_item_id="item-1")
    assert claim["status"] == "UNVERIFIED"
    assert claim["confidence"] == 0
    assert claim["verified_at"] is None
    assert claim["text"] == "El impuesto es de 3 euros"


def test_normalize_claim_ids_returns_provenance_pair():
    claim = build_claim(text="x", story_id="story-1", source_item_id="item-9")
    story, item = normalize_claim_ids(claim)
    assert story == "story-1" and item == "item-9"


def test_build_claim_strips_whitespace_and_is_deterministic():
    """Whitespace is stripped; the same logical claim yields the SAME id (H1 determinism)."""
    claim = build_claim(text="  padded text  ")
    assert claim["text"] == "padded text"
    # Deterministic identity: two calls of the same logical claim share an id, so persistence can
    # dedupe them via UNIQUE(claim_id) (idempotency, §15 / §26 Case 10). No random UUID per call.
    assert build_claim(text="same", story_id="s1")["claim_id"] == build_claim(
        text="same", story_id="s1"
    )["claim_id"]


def test_build_claim_distinct_claims_do_not_collide():
    """Two logically different claims must never share an id (H1: no false dedup)."""
    assert build_claim(text="same", story_id="s1")["claim_id"] != build_claim(
        text="other", story_id="s1"
    )["claim_id"]
    # Same text in a different story is a different claim.
    assert build_claim(text="same", story_id="s1")["claim_id"] != build_claim(
        text="same", story_id="s2"
    )["claim_id"]


def test_provenance_chain_reconstructs_full_path():
    """Story -> Source Item -> Claim -> Verification State (§2)."""
    sources_by_id = {"item-1": type("S", (), {"source_id": "src-x", "name": "Reuters-like", "tier": "TIER_2"})()}
    claim = build_claim(text="El proyecto fue aprobado.", story_id="story-1", source_item_id="item-1")
    chain = provenance_chain(claim, sources_by_id=sources_by_id)
    assert chain["story"]["id"] == "story-1"
    assert chain["source_item"]["id"] == "item-1"
    assert chain["claim"]["text"] == "El proyecto fue aprobado."
    assert chain["verification_state"] == claim["status"]


# --------------------------------------------------------------------------- #
# Verification states from fact-check results (§8) — deterministic precedence
# --------------------------------------------------------------------------- #
def test_fact_check_false_yields_contradicted():
    status, conf = aggregate_verification("UNVERIFIED", fact_results=["FALSE"])
    assert status == "CONTRADICTED" and conf == 100


def test_fact_check_true_yields_verified():
    status, conf = aggregate_verification("UNVERIFIED", fact_results=["TRUE"])
    assert status == "VERIFIED" and conf == 100


def test_unclear_without_decisive_verdict_is_partially_verified():
    status, conf = aggregate_verification("UNVERIFIED", fact_results=["UNCLEAR"])
    assert status == "PARTIALLY_VERIFIED" and conf == 50


def test_false_takes_precedence_over_true():
    """A single FALSE verdict overrides a TRUE one (section 5: never invent a resolution)."""
    status, _ = aggregate_verification("UNVERIFIED", fact_results=["TRUE", "FALSE"])
    assert status == "CONTRADICTED"


def test_no_fact_check_keeps_incoming_status_with_low_confidence():
    status, conf = aggregate_verification("PARTIALLY_VERIFIED")
    assert status == "PARTIALLY_VERIFIED" and conf == 25


# --------------------------------------------------------------------------- #
# Support / evidence (§2)
# --------------------------------------------------------------------------- #
def test_is_supported_requires_at_least_one_source_item():
    supported = build_claim(text="x", source_item_id="item-1")
    unsupported = build_claim(text="x")
    assert is_supported(supported, evidence_source_ids=["item-1"]) is True
    assert is_supported(unsupported) is False


def test_is_supported_ignores_empty_source_ids():
    claim = build_claim(text="x", source_item_id=None)
    assert is_supported(claim, evidence_source_ids=[None, "", None]) is False
