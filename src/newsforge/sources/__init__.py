"""Source Engine package (§6)."""
from newsforge.sources.engine import (  # noqa: F401
    IngestResult, dedupe_hash, fetch_url, parse_rss, parse_json_api,
    extract_from_html, parse_content, normalize_item, ingest_source,
)
from newsforge.sources.trust import (  # noqa: F401
    TIER_BASE_TRUST, tier_baseline, source_trust_score, freshness_factor,
    item_confidence, generic_phrase_ratio,
)

__all__ = [
    "IngestResult", "dedupe_hash", "fetch_url", "parse_rss", "parse_json_api",
    "extract_from_html", "parse_content", "normalize_item", "ingest_source",
    "TIER_BASE_TRUST", "tier_baseline", "source_trust_score", "freshness_factor",
    "item_confidence", "generic_phrase_ratio",
]
