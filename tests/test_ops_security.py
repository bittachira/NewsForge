"""OPS hardening — phase 1 security tests.

Covers SSRF, internal-endpoint protection, health-leakage, container non-root,
secrets, JSON-LD XSS and provider-error redaction. Everything runs OFFLINE: outbound
fetching is driven by patched resolvers/transports (no real network, no real API keys).
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from newsforge.core.netguard import SSRFError, assert_public_target, is_blocked_ip
from newsforge.sources.engine import fetch_url
from newsforge.seo.meta import render_jsonld_script


def _run(coro):
    """Run an async test body on a fresh event loop (no pytest-asyncio dependency)."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Fake aiohttp transport (used so the string "no network" is literally true)
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, status=200, *, headers=None, body=""):
        self.status = status
        self.headers = dict(headers or {})
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self, encoding="utf-8", errors="replace"):
        return self._body


class _RequestCM:
    """Mimics aiohttp._RequestContextManager: usable with both `await` and `async with`."""

    def __init__(self, coro):
        self._coro = coro

    def __await__(self):
        return self._coro.__await__()

    async def __aenter__(self):
        return await self._coro

    async def __aexit__(self, *exc):
        return False


async def _ready(value):
    return value


class _FakeSession:
    """Patches aiohttp.ClientSession: records requested URLs, serves queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requested: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        self.requested.append(str(url))
        return _RequestCM(_ready(self.responses.pop(0)))


def _patched_fetch(responses):
    session = _FakeSession(responses)
    return session, patch("newsforge.sources.engine.aiohttp.ClientSession", return_value=session)


# --------------------------------------------------------------------------- #
# 1. SSRF — IP-range classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.8.8.8", "0.0.0.0", "::1",
    "10.0.0.1", "172.16.0.1", "192.168.1.1", "100.64.0.1",
    "169.254.169.254", "169.254.1.2", "fe80::1", "fd00::1",
    "::ffff:127.0.0.1",
])
def test_ssrf_blocks_private_and_internal_ips(ip):
    assert is_blocked_ip(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "1.1.1.1",
                                "2606:4700:4700::1111"])
def test_ssrf_allows_public_ips(ip):
    assert is_blocked_ip(ip) is False


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/feed.xml",          # loopback
    "http://10.0.0.5/feed.xml",           # IPv4 private
    "http://172.16.7.7/x",                # RFC1918
    "http://192.168.0.3/x",               # RFC1918
    "http://[::1]/feed.xml",              # IPv6 loopback
    "http://[fd00::1]/feed.xml",          # IPv6 unique-local
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata / link-local
    "file:///etc/passwd",                 # non-http(s) scheme
])
def test_ssrf_assert_public_target_rejects(url):
    with pytest.raises(SSRFError):
        _run(assert_public_target(url))


def test_ssrf_assert_public_target_rejects_localhost_after_dns():
    # DNS resolution is part of the check (never a string blocklist).
    with pytest.raises(SSRFError):
        _run(assert_public_target("http://localhost/feed.xml"))


def test_ssrf_rejects_hostname_still_resolving_to_private_ip():
    with patch("newsforge.core.netguard._resolve_host", return_value=("10.0.0.7",)):
        with pytest.raises(SSRFError):
            _run(assert_public_target("http://internal.corp.example/x"))


def test_ssrf_allows_hostname_resolving_to_public_ip():
    with patch("newsforge.core.netguard._resolve_host", return_value=("93.184.216.34",)):
        _run(assert_public_target("https://example.com/feed.xml"))  # must NOT raise


def test_ssrf_allows_public_literal_ip():
    _run(assert_public_target("https://8.8.8.8/x"))  # must NOT raise


def test_fetch_url_rejects_public_to_private_redirect():
    chain = [_FakeResp(302, headers={"Location": "http://10.0.0.9/internal"})]
    session, patcher = _patched_fetch(chain)
    with patcher:
        with pytest.raises(SSRFError):
            _run(fetch_url("http://93.184.216.34/feed.xml", max_retries=1))
    # The private hop is validated BEFORE the second request is issued.
    assert session.requested == ["http://93.184.216.34/feed.xml"]


def test_fetch_url_still_fetches_public_urls():
    session, patcher = _patched_fetch([_FakeResp(200, body="<rss></rss>")])
    with patcher:
        out = _run(fetch_url("http://93.184.216.34/feed.xml", max_retries=1))
    assert out == "<rss></rss>"
    assert session.requested == ["http://93.184.216.34/feed.xml"]


# --------------------------------------------------------------------------- #
# 2. Internal endpoints / docs exposure
# --------------------------------------------------------------------------- #
def _isolated_client(db_name):
    from newsforge.db.session import use_isolated_database_ctx
    from src.newsforge.web.app import app

    db_dir = Path.cwd() / ".pytest_tmp"
    db_dir.mkdir(parents=True, exist_ok=True)
    return use_isolated_database_ctx(str(db_dir / db_name)), TestClient(app)


def test_docs_redoc_openapi_disabled():
    ctx, client = _isolated_client("sec_docs.db")
    with ctx:
        with client:
            for path in ("/docs", "/redoc", "/openapi.json"):
                assert client.get(path).status_code == 404, path


def test_analytics_fail_closed_when_no_token_configured(monkeypatch):
    monkeypatch.delenv("NEWSFORGE_ADMIN_TOKEN", raising=False)
    ctx, client = _isolated_client("sec_analytics_none.db")
    with ctx:
        with client:
            assert client.get("/analytics").status_code == 403


def test_analytics_requires_correct_token(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("sec_analytics_token.db")
    with ctx:
        with client:
            assert client.get("/analytics").status_code == 403                       # anonymous
            assert client.get("/analytics", headers={"X-Admin-Token": "wrong"}).status_code == 403
            assert client.get("/analytics", params={"token": "wrong"}).status_code == 403
            ok_header = client.get("/analytics", headers={"X-Admin-Token": "ci-admin-token"})
            assert ok_header.status_code == 200
            ok_query = client.get("/analytics", params={"token": "ci-admin-token"})
            assert ok_query.status_code == 200


# --------------------------------------------------------------------------- #
# 3. Health — no information leakage, correct 503
# --------------------------------------------------------------------------- #
def test_health_success_contract():
    ctx, client = _isolated_client("sec_health_ok.db")
    with ctx:
        with client:
            r = client.get("/health")
            assert r.status_code == 200
            assert r.json() == {"status": "ok", "db": "connected"}
            assert '"status":"ok"' in r.text and '"db":"connected"' in r.text


def test_health_failure_hides_exception_text(monkeypatch):
    import src.newsforge.web.app as web_app

    class _Boom:
        def __enter__(self):
            raise RuntimeError("SECRET-DB-LOCATION:/data/secret-x.db")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(web_app, "get_session", lambda: _Boom())
    ctx, client = _isolated_client("sec_health_fail.db")
    with ctx:
        with client:
            r = client.get("/health")
            assert r.status_code == 503
            assert r.json() == {"status": "unhealthy", "db": "disconnected"}
            assert "SECRET-DB-LOCATION" not in r.text
            assert "RuntimeError" not in r.text


# --------------------------------------------------------------------------- #
# 4. Container — non-root proof (static; the runtime proof lives in CI)
# --------------------------------------------------------------------------- #
def test_dockerfile_runs_as_non_root_user():
    full = Path("Dockerfile").read_text(encoding="utf-8")
    lines = full.splitlines()
    users = [ln.split()[1] for ln in lines if ln.strip().upper().startswith("USER ")]
    assert users, "Dockerfile must set a USER"
    assert users[-1] not in {"root", "0"}, f"image must not run as root (got {users[-1]!r})"
    assert users[-1] == "newsforge"
    # The runtime user must own /data (DB init) and /app (read).
    assert re.search(r"chown\s+-R\s+\S*\bnewsforge\S*\s+/app\s+/data", full), "chown in Dockerfile"

# --------------------------------------------------------------------------- #
# 5. Secrets — hardcoding / web exposure
# --------------------------------------------------------------------------- #
_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
    re.compile(r"\bxox[baprs]-\S+"),
]


@pytest.mark.parametrize("rel", ["Dockerfile", ".env.example", ".env.staging"])
def test_no_real_secrets_in_dockerfile_or_env_templates(rel):
    text = Path(rel).read_text(encoding="utf-8")
    for pat in _SECRET_PATTERNS:
        assert pat.search(text) is None, f"possible hardcoded secret in {rel}: {pat.pattern}"


def test_no_hardcoded_secrets_in_source_tree():
    for py in sorted(Path("src/newsforge").rglob("*.py")):
        text = py.read_text(encoding="utf-8", errors="ignore")
        for pat in _SECRET_PATTERNS:
            assert pat.search(text) is None, f"possible hardcoded secret in {py}: {pat.pattern}"


def test_api_key_and_admin_token_never_exposed_via_web(monkeypatch):
    key = "sk-webprobe00000000000000000000"
    token = "ci-admin-token"
    monkeypatch.setenv("NEWSFORGE_OPENAI_API_KEY", key)
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", token)
    ctx, client = _isolated_client("sec_web_leak.db")
    with ctx:
        with client:
            for resp in (client.get("/health"), client.get("/articles"),
                         client.get("/sitemap.xml"), client.get("/feed.xml"),
                         client.get("/analytics", headers={"X-Admin-Token": token})):
                assert key not in resp.text, "API key leaked into a HTTP response"
                assert resp.status_code < 500


# --------------------------------------------------------------------------- #
# 6. JSON-LD / XSS
# --------------------------------------------------------------------------- #
def test_jsonld_script_never_closes_script_element():
    payload = {
        "headline": '</script><script>alert(1)</script>',
        "url": "https://example.com/a?x=</script>",
    }
    out = render_jsonld_script(payload)
    assert "</script>" not in out           # the element can never be closed early
    assert "<!--" not in out                # no comment opener that could mask content
    assert "\\u003c/script>" in out
    # Still *valid* JSON-LD: JSON.parse decodes the escape back to the original text.
    assert json.loads(out)["headline"] == payload["headline"]


def test_jsonld_script_neutralizes_html_comment_opener():
    out = render_jsonld_script({"headline": "<!-- dangerous -->"})
    assert "<!--" not in out
    assert json.loads(out)["headline"] == "<!-- dangerous -->"


# --------------------------------------------------------------------------- #
# 7. Provider error redaction (key is never part of an exception message)
# --------------------------------------------------------------------------- #
def test_provider_error_redacts_key_echoed_by_server():
    from newsforge.ai import AiRouter, ProviderError
    from newsforge.config import AiConfig

    secret = "sk-redactprobe1234567890"
    router = AiRouter(config=AiConfig(
        mock=False, default_provider="openai",
        openai_api_key=secret, medium_model="gpt-4o",
    ))
    resp = MagicMock()
    resp.status_code = 500
    resp.text = f"upstream said: {secret}"  # hostile echo — must be masked
    with patch("httpx.post", return_value=resp):
        with pytest.raises(ProviderError) as ei:
            router.generate(input_text="x")
    assert secret not in str(ei.value)