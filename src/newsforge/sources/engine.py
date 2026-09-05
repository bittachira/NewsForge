"""Source Engine — ingest, normalize and deduplicate raw signals (§6).

Fetches from RSS feeds, JSON APIs and HTML pages using an async aiohttp
session (scalable for pipelines), parses them into normalized items, de-duplicates per
source and persists new rows. Never scrapes in ways that violate ToS: only
read-only GETs of public feeds/pages are performed.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import warnings
import asyncio
import aiohttp
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from datetime import datetime, timezone

from newsforge.core.logger import get_logger
from newsforge.db.session import get_session
from newsforge.db.models import SourceType, SourceTier, source_items
from newsforge.sources.trust import item_confidence

logger = get_logger("sources.engine")

DEFAULT_TIMEOUT = 15.0
MAX_RETRIES = 4
BACKOFF_BASE = 0.5


@dataclass
class IngestResult:
    added: int = 0
    skipped_dupe: int = 0
    errors: list[str] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def dedupe_hash(title: str | None, url: str | None, description: str | None = None) -> str:
    payload = f"{title or ''}|{url or ''}|{description or ''}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:32]


def _local(tag: str) -> str:
    """Return the local part of an XML tag (strips namespace)."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _first_link(soup, predicate=None):
    """Return the href of the first <a>/<link> matching ``predicate`` (or any link)."""
    for el in soup.find_all(["a", "link"]):
        if predicate is None or predicate(el):
            href = el.get("href")
            if href:
                return href.strip()
    return ""


# --------------------------------------------------------------------------- #
# Fetching with retry + backoff (§37 resilience)
# --------------------------------------------------------------------------- #
async def fetch_url(url: str, *, timeout: float = DEFAULT_TIMEOUT, max_retries: int = MAX_RETRIES) -> str:
    """Fetch a URL and return its text. Raises on persistent failure."""
    last_err: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.get(url, headers={"User-Agent": "NewsForge/0.1 (+research bot)", "Accept": "application/rss+xml, application/json, text/html,*/*"}, allow_redirects=True) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    raw = await resp.text(encoding="utf-8", errors="replace")
            return raw
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            wait = BACKOFF_BASE * (2 ** (attempt - 1))
            logger.warning("fetch %s attempt %d failed: %s; retry in %.1fs", url, attempt, exc, wait)
            await asyncio.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


# --------------------------------------------------------------------------- #
# Parsers -> normalized item dicts
# --------------------------------------------------------------------------- #
def _extract_date(value: str | None) -> str | None:
    if not value:
        return None
    # RFC 822 (common in RSS: "Fri, 05 Sep 2026 10:00:00 +0000") with/without
    # abbreviated weekday and timezone name, then ISO-8601 variants.
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%A, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(value.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    # ISO 8601 with fractional seconds / offsets.
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None


def parse_rss(raw_text: str, *, channel_title: str | None = None, channel_link: str | None = None) -> list[dict]:
    """Parse RSS 2.0 / Atom-ish XML into normalized item dicts."""
    items: list[dict] = []
    try:
        root = ET.fromstring(raw_text)
    except Exception:
        return []

    def text_of(el, name):
        for child in el.iter():
            if _local(child.tag) == name and child.text:
                return child.text.strip()
        return None

    channel = next((el for el in root.iter() if _local(el.tag) == "channel"), None)
    item_els = [el for el in root.iter() if _local(el.tag) == "item"]
    feed_el = next((el for el in root.iter() if _local(el.tag) == "feed"), None)

    if channel is not None:
        channel_title = text_of(channel, "title") or channel_title
        channel_link = text_of(channel, "link") or channel_link

    if item_els:
        for item in item_els:
            title = text_of(item, "title")
            link = text_of(item, "link") or channel_link
            description = text_of(item, "description")
            content = text_of(item, "content:encoded") or description
            published_at = _extract_date(text_of(item, "pubDate") or text_of(item, "dc:date"))
            items.append({
                "title": title, "url": link, "description": description,
                "content_html": content, "published_at": published_at,
            })
    elif feed_el is not None:  # Atom
        for entry in feed_el.findall(".//entry"):
            title = text_of(entry, "title")
            link = next((t for t in entry.iter() if _local(t.tag) == "link" and t.get("href")), channel_link)
            content = text_of(entry, "content") or text_of(entry, "summary")
            published_at = _extract_date(text_of(entry, "updated") or text_of(entry, "published"))
            items.append({
                "title": title, "url": link, "description": content,
                "content_html": content, "published_at": published_at,
            })
    else:  # generic fallback: pull <item>/<entry> loosely via bs4
        soup = BeautifulSoup(raw_text, "xml")
        for el in soup.find_all(["item", "entry"]):
            title = (el.find("title") or "").get_text(" ", strip=True)
            link = ((el.find("link") or {}).get("href") or channel_link).strip()
            desc = (el.find("description") or el.find("content") or "").get_text(" ", strip=True)
            date_el = el.find(["pubDate", "published", "updated"])
            published_at = _extract_date(date_el.get_text().strip()) if date_el else None
            items.append({"title": title, "url": link, "description": desc, "content_html": desc, "published_at": published_at})

    return [{"title": i["title"], "url": i["url"], "description": i["description"],
             "content_html": i["content_html"], "published_at": i["published_at"]} for i in items]


def parse_json_api(obj: Any) -> list[dict]:
    """Normalize a JSON API payload (object or array of objects)."""
    if isinstance(obj, dict):
        entries = obj.get("items") or obj.get("results") or [obj]
    else:
        entries = obj or []
    out: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title") or entry.get("name") or entry.get("headline")
        link = entry.get("url") or entry.get("link")
        description = entry.get("description") or entry.get("summary")
        content_html = entry.get("content") or entry.get("body") or entry.get("content_html")
        published_at = _extract_date(entry.get("publishedAt") or entry.get("date") or entry.get("published"))
        out.append({
            "title": title, "url": link, "description": description,
            "content_html": content_html, "published_at": published_at,
        })
    return out


def extract_from_html(html: str) -> dict:
    """Extract the most useful signals from an HTML page (official/gov pages)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "html.parser")
    for tag in ("script", "style", "nav", "header", "footer", "aside", "form"):
        for el in soup.find_all(tag):
            el.decompose()

    og_title = None
    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or "").lower()
        if "og:title" in prop:
            og_title = meta.get("content").strip()
            break
    title = og_title or (soup.title.string or "").strip()
    description = None
    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        if prop.startswith("og:description") or prop == "description":
            description = meta.get("content").strip()
            break

    content_parts: list[str] = []
    for el in soup.find_all(["article", "main"]):
        text = el.get_text(" ", strip=True)
        if len(text) > 120:
            content_parts.append(text)
    content_html = "\n".join(content_parts)

    return {
        "title": title,
        "url": _first_link(soup, lambda a: (a.get("rel") or "").lower() == "canonical") or _first_link(soup, lambda a: True) or "",
        "description": description,
        "content_html": content_html,
        "published_at": None,
    }


def parse_content(content_type: str | None, raw_text: str) -> list[dict]:
    """Dispatch to the right parser based on source type / URL."""
    ctype = (content_type or "").upper()
    if ctype == SourceType.API.value:
        try:
            import json as _json  # local import to avoid top-level cost
            return parse_json_api(_json.loads(raw_text))
        except Exception as exc:  # noqa: BLE001
            logger.warning("JSON parse failed for %s: %s", raw_text, exc)
            return []

    # Official / government / scientific pages are fetched and cleaned as HTML.
    if ctype in (SourceType.OFFICIAL.value, SourceType.SCIENTIFIC.value):
        data = extract_from_html(raw_text)
        return [data] if data.get("title") else []

    # Default: treat as an RSS/Atom/XML feed.
    parsed = parse_rss(raw_text)
    if not parsed:
        logger.info("No structured items extracted from %s; falling back to HTML.", raw_text[:80])
        html_data = extract_from_html(raw_text)
        return [html_data] if html_data.get("title") else []
    return parsed


# --------------------------------------------------------------------------- #
# Ingestion + deduplication
# --------------------------------------------------------------------------- #
def normalize_item(source_id: str, item: dict) -> dict:
    """Attach a dedupe hash to a raw item for de-duplication."""
    return {
        "source_id": source_id,
        "title": item.get("title"),
        "url": item.get("url"),
        "description": item.get("description"),
        "content_html": item.get("content_html"),
        "published_at": item.get("published_at"),
        "dedupe_hash": dedupe_hash(item.get("title"), item.get("url"), item.get("description")),
    }


async def ingest_source(source: dict) -> IngestResult:
    """Ingest one source into the DB (async).

    ``source`` is a row-like mapping (id/source_id/name/type/url/tier/...).
    Fetches the feed, parses items, de-duplicates per source and persists new
    rows in a single transaction; existing sources are health-checked.
    Returns an :class:`IngestResult`.
    """
    result = IngestResult()
    source_id = str(source.get("source_id") or source.get("id"))

    try:
        content_type = source.get("type") or SourceType.WEBSITE.value
        url = source.get("url")
        if not url:
            result.errors.append(f"source {source_id!r} has no url to ingest")
            return result

        raw_text = await fetch_url(url)
        items = parse_content(content_type, raw_text)
    except Exception as exc:  # noqa: BLE001
        logger.error("ingest %s failed: %s", source_id, exc)
        result.errors.append(f"fetch/parse error for {source_id}: {exc}")
        return result

    if not items:
        logger.info("No items parsed from %s (%d bytes).", url, len(raw_text))
        result.errors.append(f"no items parsed from {url}")
        return result

    # One transaction: dedupe against existing rows + insert new ones.
    try:
        with get_session() as session:
            ModelType = source_items
            existing_ids = {
                str(row.dedupe_hash)
                for row in session.query(ModelType).filter_by(source_id=source_id).all()
            }
            seen = set()
            added = skipped = 0
            for item in items:
                norm = normalize_item(source_id, item)
                key = norm["dedupe_hash"]
                if key in existing_ids or key in seen:
                    skipped += 1
                    continue
                seen.add(key)
                row = ModelType()
                row.source_id = source_id
                for col in ("title", "url", "description", "content_html", "published_at"):
                    setattr(row, col, norm.get(col))
                row.dedupe_hash = key
                session.add(row)
            session.flush()
            added = len(seen) - skipped

            # Health-check the source.
            src_row = session.query(ModelType).filter_by(source_id=source_id).first()
            if src_row:
                src_row.last_checked = datetime.now().isoformat(timespec="seconds")
            session.commit()  # persist inserts + health-check before closing
        result.added = len(seen)   # seen only holds NEW keys (dups are skipped, never added)
        result.skipped_dupe = skipped
    except Exception as exc:  # noqa: BLE001
        logger.error("ingest %s DB step failed: %s", source_id, exc)
        result.errors.append(f"db error for {source_id}: {exc}")
    return result
