"""P5 — SEO layer: JSON-LD, canonical, OpenGraph/Twitter cards, sitemap + RSS."""
from newsforge.seo.meta import (
    build_jsonld,
    canonical_url,
    open_graph_tags,
    render_jsonld_script,
    twitter_card_tags,
)
from newsforge.seo.feeds import render_rss_xml, render_sitemap_xml

__all__ = [
    "build_jsonld",
    "canonical_url",
    "open_graph_tags",
    "render_jsonld_script",
    "twitter_card_tags",
    "render_rss_xml",
    "render_sitemap_xml",
]
