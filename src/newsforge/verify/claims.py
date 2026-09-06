"""Claim Engine (§1, §2).

A *claim* is a factual assertion that can be supported or contradicted by evidence. Each
claim carries provenance: which STORY and SOURCE ITEM it came from, plus the source URL and
publication date. That provenance lets us reconstruct — deterministically — exactly why we
believe (or doubt) each claim (§2):

    Story -> Source Item -> Source -> Evidence -> Claim -> Verification State

The engine is pure and deterministic: for the same claims + evidence it always produces the
same verification state, confidence and provenance. No randomness, no "now" leakage (§15).
"""
from __future__ import annotations

import uuid

from newsforge.db.models import ClaimStatus


def build_claim(
    *,
    text: str | None,
    story_id: str | None = None,
    source_item_id: str | None = None,
    source_url: str | None = None,
    publication_date: str | None = None,
) -> dict:
    """Build a claim record. Status starts UNVERIFIED with zero confidence (§1)."""
    return {
        "claim_id": str(uuid.uuid4()),
        "story_id": story_id,
        "source_item_id": source_item_id,
        "text": (text or "").strip(),
        "source_url": source_url,
        "publication_date": publication_date,
        "verified_at": None,
        "confidence": 0,
        "status": ClaimStatus.UNVERIFIED.value,
    }


def normalize_claim_ids(claim: dict) -> tuple[str | None, str | None]:
    """Return the (story_id, source_item_id) provenance pair for a claim."""
    return claim.get("story_id"), claim.get("source_item_id")


def is_supported(claim: dict, *, evidence_source_ids=None) -> bool:
    """A claim is *supported* when at least one distinct source item backs it (§2)."""
    ids = [s for s in (evidence_source_ids or []) if s]
    return len(ids) >= 1


def aggregate_verification(claim_status: str, fact_results=None) -> tuple[str, int]:
    """Deterministically derive a verification state from authoritative fact-check results.

    Precedence (most decisive first): any FALSE result -> CONTRADICTED; else any TRUE ->
    VERIFIED; else if any UNCLEAR and no TRUE/FALSE -> PARTIALLY_VERIFIED; else keep the
    incoming status. Returns ``(status, confidence_0_to_100)`` where confidence is 100 for a
    decisive fact-check verdict and 50 for partial/unclear (§8). Pure and deterministic.
    """
    results = [str(r).upper() for r in (fact_results or []) if r]

    if "FALSE" in results:
        return ClaimStatus.CONTRADICTED.value, 100
    if "TRUE" in results:
        return ClaimStatus.VERIFIED.value, 100
    if "UNCLEAR" in results:
        return ClaimStatus.PARTIALLY_VERIFIED.value, 50

    # No authoritative fact-check verdict -> fall back to the incoming status with low confidence.
    return claim_status or ClaimStatus.UNVERIFIED.value, 25


def provenance_chain(claim: dict, *, sources_by_id: dict | None = None) -> dict:
    """Reconstruct the full provenance chain for a claim (§2).

    ``sources_by_id`` maps source_id -> source row (optional; only used to enrich the
    ``source`` node with name/tier/country when available). The returned structure always
    contains story, source_item and claim nodes so an auditor can follow it end-to-end.
    """
    sources_by_id = sources_by_id or {}
    item = claim.get("source_item_id")
    src_row = sources_by_id.get(str(item)) if item else None

    return {
        "story": {"id": claim.get("story_id"), "title": _story_title(claim)},
        "source_item": {"id": item, "url": claim.get("source_url"), "publication_date": claim.get("publication_date")},
        "source": ({"id": src_row.source_id if src_row else None, "name": getattr(src_row, "name", None),
                    "tier": getattr(src_row, "tier", None)} if src_row else None),
        "claim": {"text": claim.get("text"), "status": claim.get("status")},
        "verification_state": claim.get("status"),
    }


def _story_title(claim: dict) -> str | None:
    # Story title is not stored on the claim; a DB-backed caller can join via story_id.
    return None
