"""Story Engine — detect persistent stories from ingested signals (§4).

A *story* is a persistent entity (a ``STORY_ID``) that groups related news items
over time. When new information arrives we must be able to say: "this belongs to
the story already about X" rather than creating noise.

MVP approach (deterministic, no LLM): cluster items by **topic + year** and tag
each cluster with a dominant *entity* prefix when one is clearly shared across the
items in that cluster. This produces stable IDs like ``eu_small_package_tax_2026``.
A later phase can swap the classifier for an LLM (§4 CLUSTER step) without changing
the engine's API — only :func:`classify_topic` would change, and callers keep working.

All functions are pure so they can be unit-tested deterministically.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

# Default topic taxonomy: slug -> significant keywords (single- or multi-word).
# Multi-word keywords are matched by checking whether ANY of their significant words
# appear in the text (§51 originality / §33 multilingual recall), so Spanish and
# English variants both work. Extend via config later without touching the algorithm.
DEFAULT_TOPIC_KEYWORDS: dict[str, list[str]] = {
    "package_tax": ["package", "paquete", "small package", "impuesto", "tax"],
    "eu_policy": ["eu", "ue", "european union", "unión europea", "brussels", "eurozone", "eu policy"],
    "health": ["health", "salud", "disease", "enfermedad", "vaccine", "vacuna", "hospital", "covid", "sanidad"],
    "economy": ["economy", "economía", "inflation", "inflación", "gdp", "interest rate", "bank", "banco", "market", "mercado"],
    "technology": ["ai", "artificial intelligence", "inteligencia artificial", "tech", "software", "chip", "semiconductor", "tecnología"],
    "climate": ["climate", "clima", "carbon", "emisiones", "renewable", "renovable", "energy transition", "energía"],
    "security": ["cybersecurity", "ciberseguridad", "cyber attack", "ataque cibernético", "breach", "brecha", "hack", "malware"],
}

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "by",
    "is", "are", "was", "were", "be", "it", "that", "this", "these", "those",
    "new", "say", "said", "says", "year", "years", "day", "days", "week",
}

_WORD_RE = re.compile(r"[A-Za-z]+")
_CAPWORD_RE = re.compile(r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)")


def slugify(text: str | None, max_len: int = 60) -> str:
    """Lowercase, underscore-ize and trim a slug component."""
    if not text:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:max_len]


def significant_tokens(text: str | None) -> list[str]:
    """Lowercase word tokens (len>=3) excluding stopwords."""
    if not text:
        return []
    return [t for t in _WORD_RE.findall(text or "") if len(t) >= 3 and t.lower() not in STOPWORDS]


def classify_topic(text: str | None, topics: dict[str, list[str]] | None = None) -> tuple[str, float]:
    """Return ``(topic_slug, score)`` for the best-matching topic.

    ``score`` is the number of keyword phrases whose significant words appear in the
    text. A phrase with zero hits does **not** win — when nothing matches we return
    ``("", 0.0)`` so callers can tell "no detectable topic" apart from a real match.
    Ties break by insertion order (deterministic).
    """
    topics = topics or DEFAULT_TOPIC_KEYWORDS
    tokens = set(significant_tokens(text))
    best_slug, best_score = "", 0
    for slug, keywords in topics.items():
        score = sum(1 for kw in keywords if any(w.lower() in tokens for w in significant_tokens(kw)))
        if score > best_score:
            best_score, best_slug = score, slug
    return best_slug, float(best_score)


def extract_year(value: str | None) -> int | None:
    """Extract a 4-digit year from an ISO date string or free text."""
    if not value:
        return None
    m = re.search(r"\b(19|20)\d{2}\b", value)
    return int(m.group(0)) if m else None


def _candidate_entities(texts: list[str]) -> dict[str, int]:
    """Count multi-word capitalized phrases across a set of texts."""
    counts: dict[str, int] = {}
    for text in texts:
        if not text:
            continue
        for phrase in _CAPWORD_RE.findall(text):
            key = " ".join(phrase.split())
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def dominant_entity(texts: list[str]) -> str | None:
    """Return the multi-word capitalized phrase shared by the most distinct texts.

    Returns ``None`` when no phrase appears in >= 2 distinct texts, so callers can
    tell "a genuinely shared entity exists" from "there is none".
    """
    counts = _candidate_entities(texts)
    if not counts:
        return None
    best, best_distinct = "", 0
    for phrase, count in counts.items():
        distinct = sum(1 for t in texts if phrase in (t or ""))
        if distinct > best_distinct:
            best, best_distinct = phrase, distinct
    return best if best_distinct >= 2 else None


def detect_story_id(topic_slug: str | None, *, entity: str | None = None, year: int | None = None) -> str:
    """Build a stable STORY_ID for a topic (+ optional entity + year).

    Format mirrors the brief's example ``EU_SMALL_PACKAGE_TAX_2026``. When there is
    no topic and no shared entity we fall back to ``uncategorized_<year>`` rather than
    a bare year, so unrelated signals never collapse into an ambiguous numeric id.
    """
    parts = [p for p in (entity or "", topic_slug or "") if p]
    base = "_".join(parts)
    if not base and year:
        base = "uncategorized"
    if year:
        base += f"_{year}"
    return slugify(base).strip("_") or "story"


def classify_item(item: dict) -> dict[str, str | None]:
    """Classify a single item into its natural story key (pure; no DB access).

    Returns ``{"story_id", "topic_slug", "year"}``. Used by :func:`cluster_items` and
    the engine for incremental detection. Entity prefixes are resolved at cluster level
    (:func:`cluster_items`) so a lone item keeps a clean topic+year id.
    """
    text = (item.get("title") or "") + " " + (item.get("description") or "") + " " + (item.get("content_text") or "")
    topic_slug, _score = classify_topic(text)
    year = extract_year(item.get("published_at")) or extract_year(text)
    return {"story_id": detect_story_id(topic_slug, entity=None, year=year), "topic_slug": topic_slug, "year": year}


def cluster_items(items: list[dict]) -> dict[str, list[dict]]:
    """Group items into stories keyed by STORY_ID.

    Two-pass, deterministic clustering:

    1. Classify each item by ``(topic_slug, year)`` and group them — this is the story's
       natural key. Items with no detectable topic are grouped together under a single
       ``uncategorized_<year>`` bucket so they never masquerade as a real topic.
    2. Within each group, resolve a dominant *entity* prefix that is genuinely shared by
       >= 2 distinct items (e.g. ``European Union``). Only then does the entity become an
       ID prefix — unrelated items in different groups are never merged.

    The result is stable for a given item set and independent of input order, which keeps
    :meth:`newsforge.stories.engine.StoryDetector.process` idempotent across repeated runs.
    """
    if not items:
        return {}

    # Pass 1: classify + group by (topic_slug, year).
    groups: dict[tuple[str | None, int | None], list[dict]] = {}
    for item in items:
        text = (item.get("title") or "") + " " + (item.get("description") or "") + " " + (item.get("content_text") or "")
        topic_slug, _score = classify_topic(text)
        year = extract_year(item.get("published_at")) or extract_year(text)
        groups.setdefault((topic_slug, year), []).append(item)

    # Pass 2: resolve a shared entity prefix per group and build stable story ids.
    clusters: dict[str, list[dict]] = {}
    for (topic_slug, year), group_items in groups.items():
        texts = [t for it in group_items for t in (it.get("title") or "", it.get("description") or "", it.get("content_text") or "")]
        entity = dominant_entity(texts) if len(group_items) > 1 else None
        story_id = detect_story_id(topic_slug, entity=entity, year=year)
        clusters.setdefault(story_id, []).extend(group_items)
    return clusters
