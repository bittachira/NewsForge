"""P5 — SEO meta layer: JSON-LD, canonical URL, OpenGraph and Twitter cards.

Pure functions of their inputs (§15 determinism): the same persisted article data always
yields byte-identical markup. This layer only READS persisted state (stories /
publications / generated_artifacts) — it never regenerates content, invents facts or
mutates editorial tables; publication is downstream of the Decision Engine verdict that
the publisher already consumed (§11/§24)."""
from __future__ import annotations

import json
from typing import Optional


def canonical_url(site_url: str, slug: str) -> str:
    """Canonical absolute URL for one published article page."""
    base = (site_url or "").rstrip("/")
    return f"{base}/articles/{slug}"


def build_jsonld(*, site_name: str, title: str, summary: Optional[str],
                 url: str, published_at: Optional[str]) -> dict:
    """schema.org NewsArticle JSON-LD document for one published article.

    Only persisted fields are embedded — no derived or invented facts."""
    doc = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": title,
        "url": url,
        "mainEntityOfPage": url,
        "publisher": {"@type": "Organization", "name": site_name},
    }
    if summary:
        doc["description"] = summary
    if published_at:
        doc["datePublished"] = published_at
    return doc


def render_jsonld_script(jsonld: dict) -> str:
    """Deterministic JSON-LD payload for a <script type="application/ld+json"> tag.

    Sorted keys + compact separators => identical bytes for identical input. ``</`` and
    ``<!--`` are escaped to their JSON unicode forms so attacker-controlled text
    (titles/summaries persisting in the DB) can never close the <script> element or open
    an HTML comment inside it. The output remains valid JSON-LD (json.loads decodes the
    escapes back to the original text) while staying inert in an HTML context."""
    raw = json.dumps(jsonld, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return raw.replace("</", "\\u003c/").replace("<!--", "\\u003c!--")


def open_graph_tags(*, site_name: str, title: str, summary: Optional[str], url: str) -> list[tuple[str, str]]:
    """OpenGraph meta tags (property, content) for one article page."""
    tags = [
        ("og:type", "article"),
        ("og:site_name", site_name),
        ("og:title", title),
        ("og:url", url),
    ]
    if summary:
        tags.append(("og:description", summary))
    return tags


def twitter_card_tags(*, title: str, summary: Optional[str]) -> list[tuple[str, str]]:
    """Twitter card meta tags (name, content) for one article page."""
    tags = [("twitter:card", "summary"), ("twitter:title", title)]
    if summary:
        tags.append(("twitter:description", summary))
    return tags
