"""OPS hardening — phase 3 observability tests (OPS_HARDENING_OBSERVABILITY).

Covers request-ID correlation, structured JSON logging + redaction, the ``errors``
persistence path, /live + /ready contract, the admin-gated /metrics snapshot, metric
cardinality guards, and pipeline/AI/publish correlation. Everything runs OFFLINE (no
network, mock AI default); HTTP is exercised through FastAPI's TestClient.
"""
from __future__ import annotations

import io
import json
import logging
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import newsforge.db as db
from newsforge.db import get_session
from newsforge.db.models import SourceType, SourceTier
from newsforge.publish.destinations import (
    Destination, DistributionOutcome, register, register_builtin_destinations, reset_registry,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
_db_seq = 0


@pytest.fixture(autouse=True)
def isolated_db():
    global _db_seq
    db_dir = Path.cwd() / ".pytest_tmp"
    db_dir.mkdir(exist_ok=True)
    _db_seq += 1
    module = __name__.rsplit(".", 1)[-1]
    path = db_dir / f"{module}_{_db_seq}.db"
    try:
        if path.exists():
            path.unlink()
    except OSError:
        _db_seq += 1
        path = db_dir / f"{module}_{_db_seq}.db"
    with db.use_isolated_database_ctx(path):
        yield


@pytest.fixture(autouse=True)
def _reset_metrics():
    from newsforge.core.metrics import reset_metrics
    reset_metrics()
    yield
    reset_metrics()


@pytest.fixture(autouse=True)
def clean_registry():
    reset_registry()
    register_builtin_destinations()
    yield
    reset_registry()


def _workspace_client(monkeypatch):
    from src.newsforge.web.app import app
    return TestClient(app)


def _capture_logger(name):
    """Attach a JSON StringIO handler to ``name``; returns (logger, stream, handler)."""
    from newsforge.core.logger import JsonFormatter, get_logger
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = get_logger(name)
    logger.addHandler(handler)
    return logger, stream, handler


def _seed_source(session, *, sid: str, name: str = "Test Outlet", tier: str = "TIER_1"):
    src = db.sources()
    src.id = sid
    src.source_id = sid
    src.name = name
    src.type = "RSS"
    src.country = "ES"
    src.language = "es"
    src.tier = tier
    src.trust_score = 90 if tier == "TIER_1" else 35
    src.status = "active"
    session.add(src)
    session.commit()


def _seed_item(session, *, source_id: str, title: str, description: str | None = None) -> str:
    item = db.source_items()
    item.source_id = source_id
    item.title = title
    item.description = description
    item.content_html = None
    item.content_text = description or title
    item.published_at = "2026-09-05T10:00:00+00:00"
    item.dedupe_hash = f"hash-{title}"
    session.add(item)
    session.commit()
    return str(item.id)


def _seed_publishable_story(session, story_id="story-1"):
    st = db.stories()
    st.story_id = story_id
    st.slug = story_id
    st.title = "My Story"
    session.add(st)
    session.commit()
    dec = db.decisions(target_type="STORY", target_id=story_id,
                       decision="PUBLISH", human_override=False)
    session.add(dec)
    session.commit()
    return story_id


# --------------------------------------------------------------------------- #
# 1. Request ID correlation
# --------------------------------------------------------------------------- #
def test_request_id_generated_when_absent(monkeypatch):
    with _workspace_client(monkeypatch) as client:
        r = client.get("/health")
    rid = r.headers.get("X-Request-ID")
    assert rid and len(rid) <= 128
    # auto-generated ids are random hex (uuid4), not user-derived
    assert all(c in "0123456789abcdef" for c in rid)


def test_request_id_echoes_valid_provided_value(monkeypatch):
    with _workspace_client(monkeypatch) as client:
        r = client.get("/health", headers={"X-Request-ID": "req-custom-0001"})
    assert r.headers.get("X-Request-ID") == "req-custom-0001"


def test_request_id_replaces_invalid_and_oversized(monkeypatch):
    with _workspace_client(monkeypatch) as client:
        bad = client.get("/health", headers={"X-Request-ID": "bad\x00char"})
        huge = client.get("/health", headers={"X-Request-ID": "x" * 300})
    for r in (bad, huge):
        rid = r.headers.get("X-Request-ID")
        assert rid and len(rid) <= 128
        assert rid != "bad\x00char" and rid != "x" * 300


def test_request_id_propagates_to_structured_logs(monkeypatch):
    import src.newsforge.web.app as web_app

    class _Boom:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(web_app, "get_session", lambda: _Boom())
    logger, stream, handler = _capture_logger("web.app")
    with _workspace_client(monkeypatch) as client:
        try:
            client.get("/ready", headers={"X-Request-ID": "should-appear-123"})
        finally:
            logger.removeHandler(handler)
    lines = [json.loads(l) for l in stream.getvalue().splitlines() if l.strip()]
    readiness = [ln for ln in lines if ln.get("message", "").startswith("readiness")]
    assert readiness, lines
    assert readiness[0]["request_id"] == "should-appear-123", readiness[0]


# --------------------------------------------------------------------------- #
# 2. Structured logging + redaction
# --------------------------------------------------------------------------- #
def test_pipeline_logs_are_correlated_json():
    from newsforge.pipeline.orchestrator import run_pipeline

    with get_session() as s:
        _seed_source(s, sid="src-ok", name="Official", tier="TIER_1")
        item = _seed_item(s, source_id="src-ok", title="The tax is three euros.")

    logger, stream, handler = _capture_logger("pipeline.orchestrator")
    try:
        run_pipeline(signal_ids=[item], reference_time="2026-09-05T12:00:00+00:00")
    finally:
        logger.removeHandler(handler)

    lines = [json.loads(l) for l in stream.getvalue().splitlines() if l.strip()]
    by_event = {ln["event"]: ln for ln in lines}
    assert "pipeline_start" in by_event
    assert "pipeline_end" in by_event
    run_id = by_event["pipeline_start"]["run_id"]
    for ev in ("phase_start", "phase_end"):
        assert any(ln["event"] == ev for ln in lines)
    # Every phase line shares the same run id -> correlation works end to end.
    assert all(ln.get("run_id") == run_id for ln in lines), lines
    # Every line is a full structured record (no bare-text messages).
    for ln in lines:
        assert set(("timestamp", "level", "component", "event", "message")) <= set(ln), ln


def test_log_omits_absent_optional_fields():
    from newsforge.core.logger import JsonFormatter, get_logger

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = get_logger("unittest.optional")
    logger.addHandler(handler)
    try:
        logger.info("bare message")  # no run_id/story_id/... -> they must be OMITTED
    finally:
        logger.removeHandler(handler)
    parsed = json.loads(stream.getvalue())
    assert "run_id" not in parsed
    assert "story_id" not in parsed
    assert "request_id" not in parsed
    assert parsed["message"] == "bare message"


def test_secret_bodies_redacted_everywhere():
    from newsforge.core.logger import log_event, redact_text

    secret = "sk-verysecretbodies1234567890"
    assert "[REDACTED]" in redact_text(f"token {secret} in text")

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    from newsforge.core.logger import JsonFormatter, get_logger
    handler.setFormatter(JsonFormatter())
    logger = get_logger("unittest.redact")
    logger.addHandler(handler)
    try:
        log_event(logger, "secret_probe", message=f"leak {secret} here",
                  error_type="ValueError", error_message=f"wrapped {secret}")
    finally:
        logger.removeHandler(handler)
    out = stream.getvalue()
    assert secret not in out
    assert "[REDACTED]" in out


# --------------------------------------------------------------------------- #
# 3. Error persistence (errors table, best-effort, sanitized)
# --------------------------------------------------------------------------- #
def test_persist_error_writes_sanitized_row():
    from newsforge.core.error_tracker import persist_error

    secret = "sk-persisterr0000000000"
    persist_error(module="unittest", error_type="RuntimeError",
                  message=f"boom {secret} tail", context={"story_id": "s-9"})
    with get_session() as s:
        row = s.query(db.errors).one()
    assert "boom" in json.loads(row.context_json)["story_id"] or json.loads(row.context_json)["story_id"] == "s-9"
    assert secret not in row.message
    assert "[REDACTED]" in row.message
    assert "RuntimeError" == row.error_type
    assert row.module == "unittest"


def test_persist_error_is_best_effort_without_db(monkeypatch):
    from newsforge.core.error_tracker import persist_error
    from newsforge.db.session import is_database_initialized

    monkeypatch.setattr("newsforge.db.session.is_database_initialized", lambda: False)
    # The tracker sees "no database" and returns None without raising.
    assert persist_error(module="unittest", error_type="RuntimeError", message="x") is None


def test_error_tracking_never_breaks_caller_transaction():
    from newsforge.core.error_tracker import persist_error

    with get_session() as s:
        _seed_source(s, sid="src-tx", tier="TIER_1")
        before = s.query(db.sources).count()
    persist_error(module="unittest", error_type="ValueError", message="fail")
    with get_session() as s:
        assert s.query(db.sources).count() == before  # caller data untouched
        assert s.query(db.errors).count() == 1        # tracker row committed independently


# --------------------------------------------------------------------------- #
# 4. Liveness / readiness
# --------------------------------------------------------------------------- #
def test_live_always_alive(monkeypatch):
    with _workspace_client(monkeypatch) as client:
        r = client.get("/live")
    assert r.status_code == 200
    assert r.json() == {"status": "alive"}


def test_ready_ok_when_db_available(monkeypatch):
    with _workspace_client(monkeypatch) as client:
        r = client.get("/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "db": "connected"}


def test_ready_503_hides_exception_text(monkeypatch):
    import src.newsforge.web.app as web_app

    class _Boom:
        def __enter__(self):
            raise RuntimeError("SECRET-DB:/data/secret-x.db")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(web_app, "get_session", lambda: _Boom())
    with _workspace_client(monkeypatch) as client:
        r = client.get("/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "not ready", "db": "disconnected"}
    assert "SECRET-DB" not in r.text


# --------------------------------------------------------------------------- #
# 5. Metrics endpoint (admin gated, deterministic, no secrets/tags)
# --------------------------------------------------------------------------- #
def test_metrics_fail_closed(monkeypatch):
    monkeypatch.delenv("NEWSFORGE_ADMIN_TOKEN", raising=False)
    with _workspace_client(monkeypatch) as client:
        assert client.get("/metrics").status_code == 403


def test_metrics_requires_correct_token(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    with _workspace_client(monkeypatch) as client:
        assert client.get("/metrics").status_code == 403                        # anonymous
        assert client.get("/metrics", headers={"X-Admin-Token": "wrong"}).status_code == 403
        ok = client.get("/metrics", headers={"X-Admin-Token": "ci-admin-token"})
        assert ok.status_code == 200
        body = ok.json()
        assert set(("counters", "histograms")) <= set(body["metrics"])
        build = body["build"]
        assert "version" in build
        assert "schema_version" in build
        assert "git_commit" in build


def test_metrics_includes_http_traffic_after_requests(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    with _workspace_client(monkeypatch) as client:
        client.get("/health")
        client.get("/articles/nope")
        body = client.get("/metrics", headers={"X-Admin-Token": "ci-admin-token"}).json()
    labels = body["metrics"]["counters"]
    http = [k for k in labels if k.startswith("http_requests_total")]
    assert http, labels
    hist = [k for k in body["metrics"]["histograms"] if k.startswith("http_request_duration_ms")]
    assert hist, body["metrics"]["histograms"]


def test_request_id_is_never_a_metric_tag(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    with _workspace_client(monkeypatch) as client:
        for i in range(2):
            client.get("/health", headers={"X-Request-ID": f"req-{i}-with-high-cardinality"})
        body = client.get("/metrics", headers={"X-Admin-Token": "ci-admin-token"}).json()
    all_labels = {k: v for k, v in body["metrics"]["counters"].items()}
    all_labels.update(body["metrics"]["histograms"])
    for label in all_labels:
        assert "request_id" not in label, label
        assert "story_id" not in label, label


def test_metrics_403_does_not_leak_snapshot(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "ci-admin-token")
    with _workspace_client(monkeypatch) as client:
        r = client.get("/metrics")
    assert r.status_code == 403
    assert "schema_version" not in r.text
    assert "counters" not in r.text


# --------------------------------------------------------------------------- #
# 6. Metric cardinality guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_tags", [
    {"request_id": "x"},
    {"story_id": "s-1"},
    {"run_id": "r-1"},
    {"url": "https://example.com/a/b/c"},
    {"text": "free text"},
    {"body": "payload"},
])
def test_metric_tags_reject_high_cardinality_identifiers(bad_tags):
    from newsforge.core.metrics import metrics
    with pytest.raises(ValueError):
        metrics().inc("boom_total", tags=bad_tags)


def test_metric_tags_reject_long_values_and_too_many_tags():
    from newsforge.core.metrics import metrics, MAX_TAGS
    with pytest.raises(ValueError):
        metrics().inc("boom_total", tags={"component": "x" * 200})
    with pytest.raises(ValueError):
        metrics().inc("boom_total", tags={f"k{i}": "v" for i in range(MAX_TAGS + 1)})


# --------------------------------------------------------------------------- #
# 7. Pipeline failure correlation (errors row carries run_id)
# --------------------------------------------------------------------------- #
def test_pipeline_failure_persists_error_with_run_id(monkeypatch):
    from newsforge.pipeline.orchestrator import run_pipeline

    def _boom(*args, **kwargs):
        raise ValueError("publish blew up")

    monkeypatch.setattr("newsforge.publish.publish_story", _boom)

    with get_session() as s:
        _seed_source(s, sid="src-fail", tier="TIER_1")
        item = _seed_item(s, source_id="src-fail", title="The tax is three euros.")

    result = run_pipeline(signal_ids=[item], reference_time="2026-09-05T12:00:00+00:00")
    assert result["status"] == "ok"
    assert result["outcomes"][0].final_status == "FAILED"

    with get_session() as s:
        row = s.query(db.errors).one()
    assert row.module == "pipeline.orchestrator"
    assert row.error_type == "ValueError"
    ctx = json.loads(row.context_json)
    assert ctx["story_id"]
    assert ctx["run_id"]


# --------------------------------------------------------------------------- #
# 8. AI provider failure correlation (errors row carries request_id)
# --------------------------------------------------------------------------- #
def test_ai_provider_failure_persists_error_with_request_id():
    from newsforge.ai import AiRouter, ProviderError
    from newsforge.config import AiConfig
    from newsforge.core.request_context import request_id_var

    secret = "sk-aicorr00000000000000000000"
    router = AiRouter(config=AiConfig(
        mock=False, default_provider="openai",
        openai_api_key=secret, medium_model="gpt-4o",
    ))
    token = request_id_var.set("ai-request-77")
    try:
        with patch("httpx.post", side_effect=ConnectionError("refused")):
            with pytest.raises(ProviderError):
                router.generate(input_text="x")
    finally:
        request_id_var.reset(token)

    with get_session() as s:
        row = s.query(db.errors).one()
    assert row.module == "ai.router"
    assert row.error_type == "ProviderError"
    assert secret not in row.message
    ctx = json.loads(row.context_json)
    assert ctx["request_id"] == "ai-request-77"
    assert ctx["provider"] == "openai"


# --------------------------------------------------------------------------- #
# 9. Publisher correlation (attempt row + metric; no duplicate error rows)
# --------------------------------------------------------------------------- #
class _UnavailableDestination(Destination):
    key = "unavailable"
    name = "Offline channel"
    type = "GENERIC"

    def publish(self, payload=None):  # noqa: D102
        return DistributionOutcome(succeeded=False, error="channel offline")


class _RaisingDestination(Destination):
    key = "explodes"
    name = "Broken channel"
    type = "GENERIC"

    def publish(self, payload=None):  # noqa: D102
        raise RuntimeError("destination crashed")


def _clear_and_register(*classes):
    reset_registry()
    register_builtin_destinations()
    for cls in classes:
        register(cls.key, cls)


def test_publisher_failed_destination_records_attempt_and_metric():
    from newsforge.core.metrics import metrics
    from newsforge.publish.publisher import publish_story

    _clear_and_register(_UnavailableDestination)
    with get_session() as s:
        _seed_publishable_story(s)

    with get_session() as s:
        result = publish_story(s, story_id="story-1", destinations=["unavailable"])
    assert result["published"] is False

    with get_session() as s:
        attempt = s.query(db.publication_attempts).filter_by(destination_key="unavailable").one()
    assert str(attempt.status) == "FAILED"
    assert "channel offline" in json.loads(attempt.error_detail)["error"]

    counter = metrics().snapshot()["counters"]
    assert counter.get("publication_attempts_total{destination=unavailable,result=failed}", 0) >= 1
    # A handled destination failure is NOT a critical error row (no duplicate tracking).
    with get_session() as s:
        assert s.query(db.errors).count() == 0


def test_publisher_raising_destination_isolated_and_not_error_tracked():
    from newsforge.core.metrics import metrics
    from newsforge.publish.publisher import publish_story

    _clear_and_register(_RaisingDestination)
    with get_session() as s:
        _seed_publishable_story(s)

    with get_session() as s:
        result = publish_story(s, story_id="story-1", destinations=["explodes"])
    assert result["published"] is False
    assert result["per_destination"]["explodes"]["succeeded"] is False

    # Behaviour preserved: raised exceptions are NOT persisted as attempts (only handled
    # failures are), but the metric still records the attempt for observability.
    with get_session() as s:
        assert s.query(db.publication_attempts).filter_by(destination_key="explodes").count() == 0
        assert s.query(db.errors).count() == 0
    counter = metrics().snapshot()["counters"]
    assert counter.get("publication_attempts_total{destination=explodes,result=failed}", 0) >= 1