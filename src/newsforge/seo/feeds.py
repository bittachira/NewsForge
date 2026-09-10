"""P5 — SEO feeds: sitemap.xml and RSS 2.0.

Both are built ONLY from persisted, already-published articles (COMPLETED publications).
Pure string rendering of caller-supplied entries -> deterministic output (§15). No new
facts are invented here; titles/summaries/slugs come straight from the stored rows."""
from __future__ import annotations

from datetime import datetime
from email.utils import format_datetime
from typing import Iterable, Optional
from xml.sax.saxutils import escape


def _rfc822(iso_ts: Optional[str]) -> str:
    """ISO-8601 -> RFC 822 (GMT) for RSS <pubDate>; empty when absent."""
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_ts))
    except ValueError:
        return ""
    return format_datetime(dt, usegmt=True)


def render_sitemap_xml(entries: Iterable[dict], site_url: str) -> str:
    """Render a valid sitemap index of published article URLs.

    Each entry needs ``slug``; optional ``lastmod`` (ISO). One <url> per entry."""
    base = (site_url or "").rstrip("/")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for e in entries:
        loc = f"{base}/articles/{e['slug']}"
        lines.append("  <url>")
        lines.append(f"    <loc>{escape(loc)}</loc>")
        if e.get("lastmod"):
            lines.append(f"    <lastmod>{escape(str(e['lastmod']))}</lastmod>")
        lines.append("  </url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def render_rss_xml(*, site_name: str, site_url: str, entries: Iterable[dict]) -> str:
    """Render a valid RSS 2.0 feed of published articles.

    Each entry needs ``slug`` and ``title``; optional ``summary``/``published_at``."""
    base = (site_url or "").rstrip("/")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        f"  <title>{escape(site_name)}</title>",
        f"  <link>{escape(base + '/articles')}</link>",
        f"  <description>{escape('Published, verified articles')}</description>",
        "  <language>en</language>",
    ]
    for e in entries:
        link = f"{base}/articles/{e['slug']}"
        lines.append("  <item>")
        lines.append(f"    <title>{escape(str(e.get('title') or ''))}</title>")
        lines.append(f"    <link>{escape(link)}</link>")
        if e.get("summary"):
            lines.append(f"    <description>{escape(str(e['summary']))}</description>")
        pub = _rfc822(e.get("published_at"))
        if pub:
            lines.append(f"    <pubDate>{escape(pub)}</pubDate>")
        lines.append(f"    <guid isPermaLink=\"true\">{escape(link)}</guid>")
        lines.append("  </item>")
    lines.append("</channel>")
    lines.append("</rss>")
    return "\n".join(lines) + "\n"
