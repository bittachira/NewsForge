"""Admin manual pipeline trigger tests (HTTP -> real run_pipeline, isolated DB).

Drives the REAL app through FastAPI's TestClient: the fail-closed admin-token
gate, the POST-only route, the deliberately narrow payload schema, and the REAL
P1-P6 pipeline over an isolated SQLite DB. The happy path uses the development
default MOCK router; one test swaps in a real (non-MOCK) LM Studio-compatible
router pointed at a dead endpoint to prove that an AI failure surfaces as an
explicit FAILED outcome and NEVER silently falls back to MOCK.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import newsforge.db as db
from newsforge.ai.router import AiRouter
from newsforge.config import AiConfig
from newsforge.db import get_session, generated_artifacts, publications, stories
from newsforge.db.models import PublicationStatus
from newsforge.web import pipeline_trigger

_db_seq = 0


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
    path = db_dir / f"admin_trig_{_db_seq}_{name}.db"
    if path.exists():
        path.unlink()
    return use_isolated_database_ctx(str(path)), TestClient(app)


def _fresh_item_ts(hours_ago: int = 2) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _seed_source(session, *, sid: str, tier: str = "TIER_1", url: str | None = None):
    session.add(db.sources(
        id=sid, source_id=sid, name="Test Outlet", type="RSS", url=url,
        country="ES", language="es", tier=tier,
        trust_score=90 if tier == "TIER_1" else 35, status="active",
    ))
    session.commit()


def _seed_item(session, *, source_id: str, title: str,
               description: str | None = None) -> str:
    item = db.source_items(
        source_id=source_id, title=title, description=description,
        content_text=description or title, published_at=_fresh_item_ts(),
        dedupe_hash=f"hash-{source_id}-{title}",
    )
    session.add(item)
    session.commit()
    return str(item.id)


def _admin_headers():
    return {"X-Admin-Token": "ci-admin-token"}


# --------------------------------------------------------------------------- #
# Auth gate (fail closed, same as /analytics + /metrics)
# --------------------------------------------------------------------------- #
def test_trigger_fail_closed_when_token_not_configured(monkeypatch):
    monkeypatch.delenv("NEWSFORGE_ADMIN_TOKEN", raising=False)
    ctx, client = _isolated_client("notoken")
    with ctx:
        with client:
            assert client.post("/admin/pipeline/run").status_code == 403


def test_trigger_requires_correct_admin_token(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("token")
    with ctx:
        with client:
            assert client.post("/admin/pipeline/run").status_code == 403
            assert client.post(
                "/admin/pipeline/run",
                headers={"X-Admin-Token": "wrong"}).status_code == 403
            assert client.post(
                "/admin/pipeline/run",
                params={"token": "wrong"}).status_code == 403
            ok_header = client.post(
                "/admin/pipeline/run", headers=_admin_headers())
            assert ok_header.status_code == 200
            assert ok_header.json()["status"] in ("ok", "no-items")
            ok_query = client.post(
                "/admin/pipeline/run", params={"token": "ci-admin-token"})
            assert ok_query.status_code == 200
            assert ok_query.json()["status"] in ("ok", "no-items")


# --------------------------------------------------------------------------- #
# Method constraint (POST only)
# --------------------------------------------------------------------------- #
def test_trigger_rejects_non_post_methods(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("method")
    with ctx:
        with client:
            for call in (client.get, client.put, client.delete):
                assert call("/admin/pipeline/run",
                            headers=_admin_headers()).status_code == 405


# --------------------------------------------------------------------------- #
# Payload schema (no arbitrary commands / URLs / signals)
# --------------------------------------------------------------------------- #
def test_trigger_rejects_arbitrary_payloads(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("schema")
    with ctx:
        with client:
            for payload in ({"command": "rm -rf /"},
                            {"url": "https://evil.example/x"},
                            {"signal_ids": ["a"]},
                            {"source_id": 123},
                            ["a", "b"],
                            "not-a-dict",
                            42):
                assert client.post(
                    "/admin/pipeline/run", json=payload,
                    headers=_admin_headers()).status_code == 400, payload


def test_trigger_unknown_source_rejected(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("unknown-src")
    with ctx:
        with client:
            r = client.post(
                "/admin/pipeline/run",
                json={"source_id": "missing-uuid-xyz"}, headers=_admin_headers())
            assert r.status_code == 400
            assert "unknown source_id" in r.json()["detail"]


def test_resolve_source_builds_ingest_spec(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("resolve")
    with ctx:
        with client:
            with get_session() as s:
                _seed_source(s, sid="src-res", url="https://example.com/feed.xml")
            spec = pipeline_trigger._resolve_source("src-res")
    assert spec["source_id"] == "src-res"
    assert spec["url"] == "https://example.com/feed.xml"
    assert spec["type"] == "RSS"


def test_trigger_empty_db_returns_no_items(monkeypatch):
    """An empty database yields an explicit 'no-items' status, never 'ok'/'published'."""
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("noitems")
    with ctx:
        with client:
            r = client.post("/admin/pipeline/run", headers=_admin_headers())
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "no-items"
            assert body["stories_detected"] == 0
            assert body["stories_processed"] == 0
            assert body["published"] == 0
            assert body["outcomes"] == []

            with get_session() as s:
                assert s.query(publications).count() == 0


def test_trigger_source_without_items_returns_no_items(monkeypatch):
    """A registered source with no ingested items yields 'no-items' per source."""
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")

    async def _no_op_ingest(source):  # no network: nothing is ingested
        return type("IngestResult", (), {"errors": []})()

    monkeypatch.setattr(pipeline_trigger, "ingest_source", _no_op_ingest)
    ctx, client = _isolated_client("src-noitems")
    with ctx:
        with client:
            with get_session() as s:
                _seed_source(s, sid="src-empty-items")
            r = client.post(
                "/admin/pipeline/run",
                json={"source_id": "src-empty-items"},
                headers=_admin_headers())
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "no-items"
            assert body["source_id"] == "src-empty-items"


# --------------------------------------------------------------------------- #
# Execution (real run_pipeline over persisted items, no network)
# --------------------------------------------------------------------------- #
def test_trigger_executes_real_pipeline(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("exec")
    with ctx:
        with client:
            with get_session() as s:
                _seed_source(s, sid="src-exec", tier="TIER_1")
                _seed_item(s, source_id="src-exec", title="The tax is three euros.")

            r = client.post("/admin/pipeline/run", headers=_admin_headers())
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "ok"
            assert body["stories_detected"] >= 1
            published = [o for o in body["outcomes"]
                         if o["final_status"] == "PUBLISHED"]
            assert published, body
            assert body["ai"]["mock"] is True  # development default; E2E hook intact

            with get_session() as s:
                assert s.query(publications).filter_by(
                    status=PublicationStatus.COMPLETED.value).count() >= 1
                assert s.query(generated_artifacts).count() >= 1


def test_trigger_repeat_is_idempotent(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("idem")
    with ctx:
        with client:
            with get_session() as s:
                _seed_source(s, sid="src-idm", tier="TIER_1")
                _seed_item(s, source_id="src-idm", title="The tax is three euros.")

            r1 = client.post("/admin/pipeline/run", headers=_admin_headers())
            with get_session() as s:
                pubs_after_first = s.query(publications).filter_by(
                    status=PublicationStatus.COMPLETED.value).count()
            r2 = client.post("/admin/pipeline/run", headers=_admin_headers())
            assert r1.status_code == r2.status_code == 200
            b1, b2 = r1.json(), r2.json()
            keys1 = sorted(o["story_id"] for o in b1["outcomes"])
            keys2 = sorted(o["story_id"] for o in b2["outcomes"])
            assert keys1 == keys2

            with get_session() as s:
                pubs_after_second = s.query(publications).filter_by(
                    status=PublicationStatus.COMPLETED.value).count()
                assert pubs_after_second == pubs_after_first
                assert s.query(stories).count() == 1


def test_trigger_real_provider_no_mock_fallback(monkeypatch):
    """mock=False + a real provider that fails: content generation actually calls the
    provider (no DeterministicGenerator shortcut), the story outcome is an explicit
    FAILED carrying the ProviderError — NEVER a silent fallback to MOCK — and nothing
    is published nor charged (no ai_job is recorded for a failed generation)."""
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    real_router = AiRouter(config=replace(
        AiConfig(),
        mock=False,
        default_provider="lm_studio",
        lm_studio_base_url="http://127.0.0.1:1/v1",
        lm_studio_api_key="testing",
        small_model="test-model",
        request_timeout_s=2,
    ))
    monkeypatch.setattr(
        pipeline_trigger, "build_production_router", lambda: real_router)
    ctx, client = _isolated_client("nifallback")
    with ctx:
        with client:
            with get_session() as s:
                _seed_source(s, sid="src-nf", tier="TIER_1")
                _seed_item(s, source_id="src-nf", title="The tax is three euros.")

            r = client.post("/admin/pipeline/run", headers=_admin_headers())
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "ok"
            # Router reflects the REAL provider, not mock.
            assert body["ai"]["mock"] is False
            assert body["ai"]["provider"] == "lm_studio"
            assert body["ai"]["model"] == "test-model"
            # The dead provider was actually called -> the story FAILED explicitly,
            # carrying the provider error. No silent fallback to deterministic/MOCK.
            failed = [o for o in body["outcomes"]
                      if o["final_status"] == "FAILED"]
            assert failed, body
            assert "ProviderError" in failed[0]["error"]

            # No AI job is recorded: generation failed BEFORE cost recording, so no
            # cost/token row exists for the failed run (no generation -> no charge).
            with get_session() as s:
                from newsforge.db import ai_jobs
                assert s.query(ai_jobs).count() == 0
                assert s.query(publications).filter_by(
                    status=PublicationStatus.COMPLETED.value).count() == 0


# --------------------------------------------------------------------------- #
# Concurrency guard (no overlapping runs)
# --------------------------------------------------------------------------- #
def test_trigger_in_progress_returns_409(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    ctx, client = _isolated_client("conflict")
    acquired = pipeline_trigger._EXEC_LOCK.acquire(blocking=False)
    assert acquired, "test lock must be free"
    try:
        with ctx:
            with client:
                r = client.post("/admin/pipeline/run", headers=_admin_headers())
                assert r.status_code == 409
                assert r.json()["status"] == "already-running"
    finally:
        pipeline_trigger._EXEC_LOCK.release()