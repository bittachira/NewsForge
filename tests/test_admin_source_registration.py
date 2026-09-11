"""Admin HTTP source-registration tests (POST /admin/sources/register).

Drives the REAL FastAPI app through TestClient against an isolated SQLite DB and
exercises the fail-closed admin-token gate, POST-only constraint, the narrow
payload allowlist (no commands/extra keys), SSRF/public-target enforcement, CLI/
engine shared validation (types, source_id, duplicates), and the --verify ingest
plumbing (stubbed AND against a real loopback feed server).
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import newsforge.db as db
from newsforge.db import get_session, source_items, sources

_db_seq = 0

VALID_URL = "https://example.com/rss"
VALID_BODY = {
    "name": "Test Outlet",
    "url": VALID_URL,
    "source_id": "reg-ok",
    "type": "RSS",
    "tier": "TIER_1",
    "country": "ES",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _isolated_client(name):
    global _db_seq
    from newsforge.db.session import use_isolated_database_ctx
    from src.newsforge.web.app import app

    _db_seq += 1
    db_dir = Path.cwd() / ".pytest_tmp"
    db_dir.mkdir(parents=True, exist_ok=True)
    path = db_dir / f"src_reg_{_db_seq}_{name}.db"
    if path.exists():
        path.unlink()
    return use_isolated_database_ctx(str(path)), TestClient(app)


def _admin_headers():
    return {"X-Admin-Token": "ci-admin-token"}


def _allow_any_target(_url):
    return None


def _public_check_allowed():
    """Context manager stubbing the CLI registration-time public-host check."""
    return patch("newsforge.cli._check_public", new=_allow_any_target)


def _engine_guard_allowed():
    """Context manager stubbing the (awaited) ingest fetch guard for loopback tests."""
    async def _allow_async(_url):
        return None
    return patch("newsforge.sources.engine.assert_public_target", new=_allow_async)


def _count_sources() -> int:
    with get_session() as s:
        return s.query(sources).count()


def _find(sid: str):
    with get_session() as s:
        return s.query(sources).filter_by(source_id=sid).first()


# --------------------------------------------------------------------------- #
# Auth gate (fail closed, same as /admin/pipeline/run)
# --------------------------------------------------------------------------- #
def test_register_fail_closed_when_token_not_configured(monkeypatch):
    monkeypatch.delenv("NEWSFORGE_ADMIN_TOKEN", raising=False)
    ctx, client = _isolated_client("notoken")
    with ctx:
        with client:
            r = client.post("/admin/sources/register", json=VALID_BODY)
            assert r.status_code == 403


def test_register_requires_correct_admin_token(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("token")
    with ctx:
        with client:
            assert client.post(
                "/admin/sources/register", json=VALID_BODY).status_code == 403
            assert client.post(
                "/admin/sources/register", json=VALID_BODY,
                headers={"X-Admin-Token": "wrong"}).status_code == 403
            body_ok = dict(VALID_BODY)
            with _public_check_allowed():
                ok = client.post(
                    "/admin/sources/register", json=body_ok,
                    headers=_admin_headers())
            assert ok.status_code in (200, 201)
            body_q = dict(VALID_BODY)
            body_q["update"] = True
            with _public_check_allowed():
                ok_query = client.post(
                    "/admin/sources/register", json=body_q,
                    params={"token": "ci-admin-token"})
            assert ok_query.status_code in (200, 201)


# --------------------------------------------------------------------------- #
# Method constraint (POST only)
# --------------------------------------------------------------------------- #
def test_register_rejects_non_post_methods(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("method")
    with ctx:
        with client:
            for call in (client.get, client.put, client.delete):
                assert call("/admin/sources/register",
                            headers=_admin_headers()).status_code == 405


# --------------------------------------------------------------------------- #
# Payload allowlist (no arbitrary commands / SQL / paths / foreign params)
# --------------------------------------------------------------------------- #
def test_register_rejects_non_object_and_empty_payloads(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("shape")
    with ctx:
        with client:
            for payload in (["a"], "not-a-dict", 42, None, {}):
                r = client.post("/admin/sources/register", json=payload,
                                headers=_admin_headers())
                assert r.status_code == 400, payload
                assert r.json()["status"] == "error"


def test_register_rejects_unknown_fields(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("extra")
    with ctx:
        with client:
            for extra in (
                {"command": "rm -rf /"},
                {"signal_ids": ["a"]},
                {"trust_score": 90},
                {"no_public_check": True},
                {"check_public": False},
                {"sql": "SELECT * FROM sources"},
                {"file_path": "/app/src/secret.py"},
                {"language": "en"},
            ):
                body = dict(VALID_BODY)
                body.update(extra)
                r = client.post("/admin/sources/register", json=body,
                                headers=_admin_headers())
                assert r.status_code == 400, extra
                assert "unsupported payload fields" in r.json()["detail"]


def test_register_rejects_wrong_field_types(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("types")
    with ctx:
        with client:
            for key, value in (("name", 42), ("url", 123), ("source_id", 5),
                               ("type", ["RSS"]), ("tier", 1),
                               ("country", True), ("update", "yes"),
                               ("verify", 1)):
                body = dict(VALID_BODY)
                body[key] = value
                r = client.post("/admin/sources/register", json=body,
                                headers=_admin_headers())
                assert r.status_code == 400, (key, value)


def test_register_requires_name_and_url(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("required")
    with ctx:
        with client:
            for missing in ("name", "url"):
                body = {k: v for k, v in VALID_BODY.items() if k != missing}
                r = client.post("/admin/sources/register", json=body,
                                headers=_admin_headers())
                assert r.status_code == 400, missing


# --------------------------------------------------------------------------- #
# Validation reuse (CLI/Source engine): SSRF, types, source_id, duplicates
# --------------------------------------------------------------------------- #
def test_register_rejects_private_url_via_real_ssrf_guard(monkeypatch):
    """No patching: the real assert_public_target refuses a loopback literal offline."""
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("ssrf")
    with ctx:
        with client:
            body = dict(VALID_BODY)
            body["url"] = "http://127.0.0.1/feed.xml"
            r = client.post("/admin/sources/register", json=body,
                            headers=_admin_headers())
            assert r.status_code == 400
            assert "non-public address" in r.json()["detail"]
            assert _count_sources() == 0


def test_register_rejects_non_http_scheme(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("scheme")
    with ctx:
        with client:
            body = dict(VALID_BODY)
            body["url"] = "file:///C:/secret.txt"
            r = client.post("/admin/sources/register", json=body,
                            headers=_admin_headers())
            assert r.status_code == 400
            assert "non-http(s) scheme" in r.json()["detail"]


def test_register_rejects_invalid_type_and_tier(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("enum")
    with ctx:
        with client:
            for key, value in (("type", "SCAM"), ("tier", "TIER_9")):
                body = dict(VALID_BODY)
                body[key] = value
                r = client.post("/admin/sources/register", json=body,
                                headers=_admin_headers())
                assert r.status_code == 400
                assert "unknown --" in r.json()["detail"]


def test_register_rejects_invalid_source_id(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("sid")
    with ctx:
        with client:
            for bad in ("../../etc/passwd", "has space", "slash/thing",
                        "semi;colon", 'quote"x', "$PATH"):
                body = dict(VALID_BODY)
                body["source_id"] = bad
                r = client.post("/admin/sources/register", json=body,
                                headers=_admin_headers())
                assert r.status_code == 400, bad
                assert "source_id" in r.json()["detail"]


def test_register_creates_source(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("create")
    with ctx:
        with client:
            with _public_check_allowed():
                r = client.post("/admin/sources/register", json=VALID_BODY,
                                headers=_admin_headers())
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "created"
            assert body["source_id"] == "reg-ok"
            assert body["name"] == "Test Outlet"
            assert body["type"] == "RSS"
            assert body["tier"] == "TIER_1"
            assert body["verify"] is None
            row = _find("reg-ok")
            assert row is not None
            assert row.url == VALID_URL
            assert row.country == "ES"
            assert _count_sources() == 1


def test_register_duplicate_rejected_without_update(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("dup")
    with ctx:
        with client:
            with _public_check_allowed():
                assert client.post("/admin/sources/register", json=VALID_BODY,
                                   headers=_admin_headers()).status_code == 200
                r = client.post("/admin/sources/register", json=VALID_BODY,
                                headers=_admin_headers())
            assert r.status_code == 400
            assert "SOURCE_EXISTS" in r.json()["detail"]
            assert _count_sources() == 1


def test_register_update_overwrites_fields(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("upd")
    with ctx:
        with client:
            with _public_check_allowed():
                assert client.post("/admin/sources/register", json=VALID_BODY,
                                   headers=_admin_headers()).status_code == 200
                body = dict(VALID_BODY)
                body["name"] = "Renamed"
                body["url"] = "https://example.com/new"
                body["tier"] = "TIER_2"
                body["update"] = True
                r = client.post("/admin/sources/register", json=body,
                                headers=_admin_headers())
            assert r.status_code == 200
            assert r.json()["status"] == "updated"
            row = _find("reg-ok")
            assert row.name == "Renamed"
            assert row.url == "https://example.com/new"
            assert row.tier == "TIER_2"
            assert _count_sources() == 1


def test_schema_not_ready_returns_503(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("noschema")
    with ctx:
        with client:
            with get_session() as s:
                s.execute(text("DROP TABLE sources"))
                s.commit()
            with _public_check_allowed():
                r = client.post("/admin/sources/register", json=VALID_BODY,
                                headers=_admin_headers())
            assert r.status_code == 503
            assert "schema not ready" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# verify=true -> the real ingest engine
# --------------------------------------------------------------------------- #
def test_verify_uses_ingest_engine_stub(monkeypatch):
    """verify=true routes to newsforge.cli.verify_source -> ingest_source."""
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("vstub")
    with ctx:
        with client:
            calls: list[dict] = []

            from newsforge.sources.engine import IngestResult

            async def _fake_ingest(src):
                calls.append(dict(src))
                return IngestResult(added=3, skipped_dupe=1)

            monkeypatch.setattr("newsforge.cli.ingest_source", _fake_ingest)
            body = dict(VALID_BODY)
            body["verify"] = True
            with _public_check_allowed():
                r = client.post("/admin/sources/register", json=body,
                                headers=_admin_headers())
            assert r.status_code == 200
            assert r.json()["verify"] == {"status": "ok", "added": 3, "skipped": 1}
            assert calls and calls[0]["source_id"] == "reg-ok"
            assert calls[0]["url"] == VALID_URL


RSS_SAMPLE = """<?xml version="1.0"?>
<rss>
  <channel>
    <title>Test Feed</title>
    <link>http://example.test</link>
  </channel>
  <item>
    <title>Nueva ley aprobada</title>
    <link>http://example.test/1</link>
    <description>Primera noticia de prueba.</description>
  </item>
  <item>
    <title>Segunda noticia</title>
    <link>http://example.test/2</link>
    <description>Segunda historia de prueba.</description>
  </item>
</rss>"""


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


def test_verify_runs_real_ingest_engine(rss_server, monkeypatch):
    """verify=true actually persists items through the existing SSRF-guarded engine.

    The engine's guard is bypassed HERE ONLY so the loopback fixture can be used
    (product-level SSRF strictness is asserted above and in test_ops_security).
    Registration's own public-target check is stubbed the same way as the other
    creation tests."""
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("vreald")
    with ctx:
        with client:
            body = dict(VALID_BODY)
            body["url"] = rss_server
            body["verify"] = True
            with _public_check_allowed(), _engine_guard_allowed():
                r = client.post("/admin/sources/register", json=body,
                                headers=_admin_headers())
            assert r.status_code == 200
            assert r.json()["verify"]["status"] == "ok"
            assert r.json()["verify"]["added"] == 2
            with get_session() as s:
                assert s.query(source_items).filter_by(
                    source_id="reg-ok").count() == 2
            # Re-verify: dedupe makes it a no-op (engine idempotency).
            body["update"] = True
            with _public_check_allowed(), _engine_guard_allowed():
                r2 = client.post("/admin/sources/register", json=body,
                                 headers=_admin_headers())
            assert r2.json()["verify"] == {"status": "ok", "added": 0, "skipped": 2}