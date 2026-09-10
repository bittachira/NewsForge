"""Automated tests for the Source Engine (§6) — parsers, trust scoring, ingest + dedup."""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

import newsforge.db as db
from newsforge.db import get_session, source_items
from newsforge.sources import (
    dedupe_hash,
    extract_from_html,
    freshness_factor,
    item_confidence,
    ingest_source,
    parse_content,
    parse_json_api,
    parse_rss,
    source_trust_score,
    tier_baseline,
)


RSS_SAMPLE = """<?xml version="1.0"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Test Feed</title>
    <link>http://example.test</link>
  </channel>
  <item>
    <title>Nuevo impuesto aprobado</title>
    <link>http://example.test/1</link>
    <description>El gobierno aprueba un impuesto.</description>
    <pubDate>Fri, 05 Sep 2026 10:00:00 +0000</pubDate>
  </item>
  <item>
    <title>Otra noticia</title>
    <link>http://example.test/2</link>
    <description>segunda historia.</description>
  </item>
</rss>"""

HTML_SAMPLE = (
    '<html><head><title>OG</title></head>'
    '<body><meta property="og:title" content="OG Title">'
    '<article>Lorem ipsum dolor sit amet consectetur adipiscing elit. '
    'Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. '
    'Ut enim ad minim veniam quis nostrud exercitation ullamco laboris.</article>'
    '</body></html>'
)

JSON_API_SAMPLE = {"items": [{"title": "A", "url": "u1", "description": "d1"}, {"title": "B", "url": "u2"}]}


@pytest.fixture(autouse=True)
def isolated_db():
    """Give every test its own throwaway database on the same drive as cwd,
    restoring the default afterwards.

    SQLAlchemy's sqlite:// URL scheme can't open cross-drive absolute paths on
    Windows, so we keep the isolated DB under the project root (same drive).
    """
    import shutil
    from pathlib import Path

    db_dir = Path.cwd() / ".pytest_tmp"
    # Start from a clean dir every run so an interrupted previous run can't
    # leave a locked/accumulating DB behind.
    shutil.rmtree(db_dir, ignore_errors=True)
    db_dir.mkdir(exist_ok=True)
    path = db_dir / "test.db"
    with db.use_isolated_database_ctx(path):
        yield
    # Cleanup: the previous engine was disposed on switch, so its file lock is free.
    shutil.rmtree(db_dir, ignore_errors=True)


def _count_items():
    with get_session() as s:
        return s.query(source_items).count()


# --------------------------------------------------------------------------- #
# Parsers (deterministic, offline)
# --------------------------------------------------------------------------- #
def test_parse_rss_counts_and_titles():
    items = parse_rss(RSS_SAMPLE)
    assert len(items) == 2
    assert items[0]["title"] == "Nuevo impuesto aprobado"
    assert items[0]["url"] == "http://example.test/1"
    assert items[0]["published_at"] is not None


def test_parse_json_api():
    items = parse_json_api(JSON_API_SAMPLE)
    assert len(items) == 2
    assert items[0]["title"] == "A" and items[1]["title"] == "B"


def test_extract_from_html_og_and_content():
    data = extract_from_html(HTML_SAMPLE)
    assert "OG Title" in data["title"]
    assert len(data["content_html"]) > 120


def test_dedupe_hash_is_stable_and_order_independent():
    a = dedupe_hash("T", "U", "D")
    b = dedupe_hash("T", "U", "D")
    c = dedupe_hash("different", "U", "D")
    assert a == b
    assert a != c


def test_trust_scoring():
    assert tier_baseline("TIER_1") > tier_baseline("TIER_3") > tier_baseline("TIER_4")
    fresh = source_trust_score("TIER_1", "2026-09-05T00:00:00+00:00")
    stale = source_trust_score("TIER_1", "2026-07-01T00:00:00+00:00")
    assert fresh > stale  # recency penalty for stale sources
    assert item_confidence(fresh) >= item_confidence(35)


# --------------------------------------------------------------------------- #
# Ingest + dedup (against a local HTTP server — fully offline & deterministic)
# --------------------------------------------------------------------------- #
class _RssHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = RSS_SAMPLE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence test noise
        pass


@pytest.fixture(scope="module")
def rss_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _RssHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/feed.xml"
    srv.shutdown()


async def _allow_any_target(_url):
    return None


def _ingest_with_local_fetch(source):
    """Run ingest_source against the loopback test server.

    The SSRF guard (assert_public_target) is bypassed HERE ONLY so these tests keep
    exercising ingest + dedup logic against a local fixture. Product-level SSRF
    strictness (localhost/private targets blocked) is asserted in test_ops_security.
    """
    with patch("newsforge.sources.engine.assert_public_target", new=_allow_any_target):
        return asyncio.run(ingest_source(source))


def test_ingest_adds_items(rss_server):
    source = {"source_id": "local-rss", "name": "Local RSS", "url": rss_server, "type": "RSS", "tier": "TIER_2"}
    result = _ingest_with_local_fetch(source)
    assert result.added == 2
    assert result.errors == []
    assert _count_items() == 2


def test_ingest_dedupes_on_reingest(rss_server):
    source = {"source_id": "local-rss-2", "name": "Local RSS 2", "url": rss_server, "type": "RSS", "tier": "TIER_2"}
    first = _ingest_with_local_fetch(source)
    second = _ingest_with_local_fetch(source)
    assert first.added == 2 and first.skipped_dupe == 0
    # Re-ingesting identical content must be a no-op (dedup works).
    assert second.added == 0 and second.skipped_dupe == 2


def test_parse_content_dispatch_by_type():
    rss = parse_content("RSS", RSS_SAMPLE)
    assert len(rss) == 2
    official = parse_content("OFFICIAL", HTML_SAMPLE)
    assert len(official) == 1 and "OG Title" in official[0]["title"]
