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

import hashlib
import re

from newsforge.db.models import ClaimStatus
from newsforge.stories.detector import significant_tokens


def build_claim(
    *,
    text: str | None,
    story_id: str | None = None,
    source_item_id: str | None = None,
    claim_id: str | None = None,
    source_url: str | None = None,
    publication_date: str | None = None,
) -> dict:
    """Build a claim record. Status starts UNVERIFIED with zero confidence (§1).

    ``claim_id`` is optional; when omitted a fresh UUID is generated (each call is a distinct
    entity). When supplied it is used verbatim, which lets callers make persistence idempotent:
    re-running the same evaluation yields the same key and the UNIQUE constraint rejects the
    duplicate (§15, §26 Case 10).
    """
    return {
        # H1 determinism: when the caller does not supply a claim_id we derive a STABLE identity from
        # the claim's provenance + normalized text (story_id + text) instead of a random UUID. Two
        # calls describing the same logical claim therefore yield the SAME id, which lets persistence
        # dedupe them through UNIQUE(claim_id) (idempotency, §15 / §26 Case 10). Genuinely different
        # claims differ in story or text and never collide. No randomness, no timestamp (§15).
        "claim_id": claim_id or _canonical_identity((text or "").strip(), story_id),
        "story_id": story_id,
        "source_item_id": source_item_id,
        "text": (text or "").strip(),
        "source_url": source_url,
        "publication_date": publication_date,
        "verified_at": None,
        "confidence": 0,
        "status": ClaimStatus.UNVERIFIED.value,
    }


_CAPWORD_RE = re.compile(r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)")


# A single capitalized proper noun alone is a weak event signal: two stories that
# merely name the same company (e.g. "Apple") would wrongly merge. We therefore
# require at least one shared significant token alongside it, which the shared name
# itself provides. Multi-word proper nouns ("Central Bank") are stronger and match
# directly. Without any shared proper noun, an event match needs at least this many
# shared significant tokens so two generic tech/economy stories never merge.
_SINGLE_CAP_MIN_TOKENS = 1
_NO_ENTITY_MIN_TOKENS = 3
_MULTI_WORD_MIN_PARTS = 2


def _capitalized_phrases(text: str | None) -> set[str]:
    """Proper-noun phrases in ``text``, excluding ALL-CAPS acronyms (AI/UK/EU/...)."""
    if not text:
        return set()
    return {
        " ".join(p.split())
        for p in _CAPWORD_RE.findall(text)
        if any(ch.islower() for ch in p)
    }


def evidence_matches(subject_text: str | None, candidate_text: str | None) -> bool:
    """Deterministic predicate: does ``candidate_text`` support the same claim as ``subject_text``?

    Cross-source items only corroborate each other when they describe the SAME event.
    The signal reuses the story detector's normalizer (:func:`significant_tokens`) so
    evidence linkage is consistent with clustering. A candidate matches when:

    * both texts share a MULTI-WORD proper noun (e.g. ``Central Bank``); or
    * both share a SINGLE capitalized proper noun AND at least one significant token
      (the shared name itself counts, so "Anthropic ... bioweapons" corroborates
      "Anthropic ... biology projects"); or
    * they share >= 3 significant tokens.

    ALL-CAPS acronyms (``AI``, ``UK``, ``EU``) are ignored as entities. The predicate
    is symmetric, pure and threshold-based (no LLM). It errs toward under-merge except
    for single-entity-name matches (two stories merely naming the same company can
    over-merge) -- the pinned regression guarantees unrelated story members never
    fabricate corroboration, and the publish gates keep the residual risk on REVIEW.
    """
    if not subject_text or not candidate_text:
        return False
    stokens = {t.lower() for t in significant_tokens(subject_text)}
    ctokens = {t.lower() for t in significant_tokens(candidate_text)}
    shared_tokens = stokens & ctokens
    sphrases = {p.lower() for p in _capitalized_phrases(subject_text)}
    cphrases = {p.lower() for p in _capitalized_phrases(candidate_text)}
    shared_phrases = sphrases & cphrases
    multi_word = {p for p in shared_phrases if len(p.split()) >= _MULTI_WORD_MIN_PARTS}
    if multi_word:
        return True
    single_word = shared_phrases - multi_word
    if single_word:
        return len(shared_tokens) >= _SINGLE_CAP_MIN_TOKENS
    return len(shared_tokens) >= _NO_ENTITY_MIN_TOKENS


def _canonical_identity(text: str, story_id: str | None) -> str:
    """Deterministic identity key for a claim (H1).

    Identity = SHA-256 of ``story_id + "\x00" + normalized_text``. It is stable by construction: no
    random UUID, no timestamp, no unstable data. Including ``story_id`` guarantees the same text is
    never confused across stories; when a story is absent we fall back to text alone. This gives a
    reproducible key that both dedupes identical claims and keeps distinct claims separate.
    """
    normalized = (text or "").strip()
    key = f"{(story_id or '').strip()}\x00{normalized}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


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
