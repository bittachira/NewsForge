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


# Feed boilerplate from real Gazette/BBC/Guardian exports: HTML tags, "Continue
# reading", newsletter signup shims and generic navigation text are stripped BEFORE
# entity/token extraction so they can never fabricate a shared signal.
_BOILERPLATE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bcontinue\s+reading(?:\s+the\s+main\s+story)?\b[.\s]*", re.IGNORECASE),
    re.compile(r"\bsign\s+up\s+(?:to|for)\b[^.]*\.", re.IGNORECASE),
    re.compile(r"\bavailable\s+for\s+everyone,?\s*funded\s+by\s+readers\b[^.]*\.", re.IGNORECASE),
    re.compile(r"\bthis\s+article\s+is\s+more\s+than\b[^.]*\.", re.IGNORECASE),
    re.compile(r"\b(?:read|show|see|find\s+out)\s+more\b", re.IGNORECASE),
    re.compile(r"\bthe\s+latest\s+news\b[^.]*\.", re.IGNORECASE),
]

# Generic tokens that are too weak to corroborate an event even when shared across
# sources: common "meta" tech/business vocabulary, pronouns, modals and filler set by
# every outlet. Entities and event-specific signals are extracted AFTER filtering this
# list, so a shared ``ai``/``report``/``company`` never merges unrelated stories.
_GENERIC_SIGNAL_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for", "with",
    "by", "from", "as", "is", "are", "was", "were", "be", "been", "being", "am", "has",
    "have", "had", "do", "does", "did", "will", "would", "can", "could", "may", "might",
    "shall", "should", "must", "not", "no", "nor", "so", "than", "that", "this", "these",
    "those", "there", "here", "it", "its", "he", "she", "we", "us", "our", "they",
    "them", "their", "you", "your", "i", "me", "my", "who", "whom", "whose", "which",
    "what", "when", "where", "why", "how", "then", "now", "up", "down", "out", "into",
    "over", "under", "off", "about", "after", "before", "during", "within", "between",
    "again", "once", "even", "just", "also", "only", "very", "still", "yet", "too",
    "much", "many", "most", "more", "some", "any", "each", "every", "both",
    "one", "two", "first", "last", "new", "best", "top", "latest",
    "say", "says", "said", "told", "reports", "report", "according", "news", "story",
    "tech", "technology", "technologies", "digital", "online", "internet",
    "web", "data", "app", "apps", "software", "hardware", "computer", "computers",
    "company", "companies", "firm", "firms", "business", "businesses", "startup",
    "startups", "industry", "market", "markets", "product", "products", "price",
    "prices", "user", "users", "customer", "customers", "people", "person", "year",
    "years", "month", "months", "week", "weeks", "day", "days", "time", "times",
    "today", "tomorrow", "yesterday", "world", "global", "nation", "countries",
    "country", "usa", "uk", "eu", "europe", "government", "official",
    "artificial", "intelligence", "ai", "llm", "chatbot", "chatbots", "robot",
    "robots", "security", "safety", "study", "research",
    "researchers", "researcher", "scientist", "scientists", "expert", "experts", "analysis",
    "announcement", "developments", "update", "updates", "version", "versions",
    "releases", "release", "launch", "launches", "placed", "plans", "plan",
    "million", "billions", "billion", "pound", "pounds", "euros", "dollar",
    "dollars", "per", "cent", "percent", "since", "while", "against", "through",
    "despite", "because", "though", "although", "if", "whether", "unless",
    "nearly", "almost", "around", "approximately", "roughly", "least", "without", "across", "toward", "towards", "inside", "behind", "beyond",
    "amid", "reportedly", "confirmed", "minutes", "hours", "annual", "quarter",
    "quarterly", "monthly", "weekly", "daily", "average", "expected", "fastest",
    "leading", "popular", "advanced", "advancements", "models", "model", "tools",
    "tool", "features", "feature", "offers", "offer", "brings", "bring", "makes",
    "make", "work", "works", "working", "lives", "living", "life", "real",
    "actual", "same", "different", "other", "another",
    # Generic risk/safety vocabulary shared by every Anthropic-style safety story.
    "threat", "threats", "warn", "warns", "warned", "warning", "warnings", "risk",
    "risks", "unsafe", "perils", "concern", "concerns", "fear", "fears", "danger",
    "dangers", "all", "come", "comes", "use", "used", "using",
    # Product-review vocabulary: phone stories would otherwise merge on these.
    "phone", "phones", "camera", "cameras", "battery", "screen", "display",
    "processor", "performance", "quality", "review", "reviews", "cost",
    "pro", "max", "flagship", "design", "device", "devices", "smartphone", "bad",
})
# Four-digit year tokens are shared by nearly every feed item and carry no signal.
_YEAR_TOKEN_RE = re.compile(r"^(?:19|20)\d{2}$")
# Pure-numeric tokens (model numbers, specs, prices, counts) carry no event signal.
_NUMERIC_TOKEN_RE = re.compile(r"^[0-9]+$")

# HTML tags are stripped before any matching so markup can never leak tokens.
_HTML_TAG_RE = re.compile(r"<[^>]+>")


# A shared proper noun establishes the entity (who/what the event is about), but a
# SINGLE company/app name alone is a weak event signal (an "Apple" investment story
# and an "Apple" product review would wrongly merge). Corroboration therefore needs:
#
#   * a shared MULTI-WORD proper noun (e.g. "Central Bank")  -> direct match; or
#   * a shared SINGLE proper noun AND >= 1 shared significant (generic-filtered)
#     non-entity token drawn from the item's TITLE plus its DESCRIPTION. The
#     description is *controlled support*: it may supply the extra signal ("former",
#     "missiles") but never the event identity on its own.
#
# Without a shared entity there is NO fallback (the old global ">= 3 shared tokens"
# rule is gone). Entity extraction only considers capitalized words inside the TITLE,
# so sentence-initial "Is", boilerplate fragments and acronyms (AI/UK/EU) can't count.
_MULTI_WORD_MIN_PARTS = 2


def _clean_text(text: str | None) -> str:
    """Strip HTML and feed boilerplate, then collapse whitespace."""
    if not text:
        return ""
    cleaned = _HTML_TAG_RE.sub(" ", text)
    for _ in range(3):
        updated = cleaned
        for pattern in _BOILERPLATE_PATTERNS:
            updated = pattern.sub(" ", updated)
        if updated == cleaned:
            break
        cleaned = updated
    return " ".join(cleaned.split())


def _significant_set(text: str | None) -> set[str]:
    """Generic-filtered lowercase keywords in ``text`` (signal tokens)."""
    if not text:
        return set()
    cleaned = _clean_text(text).lower().replace("'s", "")
    return {
        t for t in re.findall(r"[a-z0-9]+", cleaned)
        if len(t) >= 2
        and t not in _GENERIC_SIGNAL_WORDS
        and not _YEAR_TOKEN_RE.match(t)
        and not _NUMERIC_TOKEN_RE.match(t)
    }


def _title_entities(title: str | None) -> set[str]:
    """Proper-noun phrases in the TITLE only, with weak capitalizations filtered.

    Skips ALL-CAPS acronyms (AI/UK/EU), phrases whose leading word is a generic
    sentence-starter ("Is", "Does", "How", "Will") and phrases where EVERY token is
    generic filler (e.g. "The") -- those describe no real entity.
    """
    if not title:
        return set()
    phrases: set[str] = set()
    for match in _CAPWORD_RE.finditer(_clean_text(title)):
        phrase = " ".join(match.group(1).split())
        if not any(ch.islower() for ch in phrase):
            continue  # ALL-CAPS acronym
        tokens = [t.lower() for t in phrase.split()]
        if not tokens or tokens[0] in _GENERIC_SIGNAL_WORDS:
            continue
        if all(t in _GENERIC_SIGNAL_WORDS for t in tokens):
            continue
        phrases.add(phrase.lower())
    return phrases


def evidence_matches(
    *,
    subject_title: str | None,
    subject_description: str | None,
    candidate_title: str | None,
    candidate_description: str | None,
) -> bool:
    """Deterministic predicate: do two cross-source items support the SAME event?

    Cross-source items only corroborate each other when they describe the SAME event.
    Entity identity comes from the TITLE; the description plays a strictly *controlled
    support* role (it can supply the extra corroborating signal, never the identity).
    A candidate matches when:

    * both TITLES share a MULTI-WORD proper noun (e.g. ``Central Bank``); or
    * both TITLES share a SINGLE proper noun AND the items share >= 1 significant
      generic-filtered non-entity signal across title+description (e.g. BBC "Anthropic
      blocks ... biological weapons" x Guardian "Anthropic details ... bioweapons"
      corroborate via ``former``, while a Google investment story and a Google phone
      review share only the name and do NOT).

    Sentence-initial capitalized words ("Is", "Does"), ALL-CAPS acronyms (AI/UK/EU),
    generic tech/business vocabulary, feed boilerplate and HTML markup are filtered out
    before matching, so "3 shared words" alone NEVER matches. The predicate is
    symmetric, pure and threshold-based (no LLM).
    """
    if not subject_title or not candidate_title:
        return False
    shared_entities = _title_entities(subject_title) & _title_entities(candidate_title)
    if not shared_entities:
        return False

    entity_words = set()
    for phrase in shared_entities:
        entity_words.update(phrase.split())

    multi_word = {p for p in shared_entities if len(p.split()) >= _MULTI_WORD_MIN_PARTS}
    if multi_word:
        return True

    # Single-entity match: need one shared non-entity signal from title+description.
    subject_pool = (_significant_set(subject_title) | _significant_set(subject_description)) - entity_words
    candidate_pool = (_significant_set(candidate_title) | _significant_set(candidate_description)) - entity_words
    return bool(subject_pool & candidate_pool)


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
