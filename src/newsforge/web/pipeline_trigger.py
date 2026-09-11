"""Admin-gated manual trigger for the real P1-P6 pipeline (production OPS).

This module provides the execution core behind ``POST /admin/pipeline/run``.
Authentication lives in newsforge.web.app (``_internal_allowed`` — the same
fail-closed admin-token gate used by /analytics and /metrics) and is enforced
BEFORE anything from this module runs.

Contract
--------
* POST only (FastAPI answers 405 for every other method).
* The request body is OPTIONAL and a deliberately narrow JSON object:
  ``{"source_id": "<existing sources.source_id>"}``. Anything else — extra keys,
  non-object payloads, arbitrary URLs or commands — is rejected with HTTP 400.
* With ``source_id``: the registered source is ingested (SSRF-guarded fetch,
  P1) and the pipeline runs over that source's persisted source_items.
* Without ``source_id``: the pipeline runs over ALL persisted source_items
  (an empty DB yields an empty, safe result — never an error). Callers can
  never select arbitrary signals or URLs.
* The AiRouter is built from env at request time. MOCK cannot be active in a
  production environment (enforced by AiRouter / the startup gate), so a real
  provider failure surfaces as an explicit ``FAILED`` outcome with the error —
  there is NEVER a silent fallback to MOCK.
* Runs are serialized process-wide: a concurrent trigger returns HTTP 409 (no
  overlapping runs, so nothing in flight can create duplicate publications).
  Re-running the same inputs afterwards is idempotent (source dedupe +
  deterministic cluster keys + UNIQUE constraints in P2-P5).
"""
from __future__ import annotations

import asyncio
import threading

from fastapi.responses import JSONResponse

from newsforge.ai.router import AiRouter
from newsforge.config import AiConfig
from newsforge.core.error_tracker import persist_error
from newsforge.core.logger import get_logger, log_event
from newsforge.core.request_context import get_request_id
from newsforge.db import get_session, source_items, sources
from newsforge.pipeline.orchestrator import PipelinePhaseError, run_pipeline
from newsforge.sources.engine import ingest_source

logger = get_logger("web.pipeline_trigger")

# Process-local serialization: at most ONE pipeline run per worker at a time.
_EXEC_LOCK = threading.Lock()

_ALLOWED_PAYLOAD_KEYS = frozenset({"source_id"})


def validate_payload(payload) -> str | None:
    """Validate the OPTIONAL request body -> ``source_id`` (or ``None``).

    Raises :class:`ValueError` with a user-safe message for every rejected shape.
    """
    if payload is None or payload == {}:
        return None
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    extra = set(payload) - _ALLOWED_PAYLOAD_KEYS
    if extra:
        raise ValueError(f"unsupported payload fields: {', '.join(sorted(extra))}")
    raw = payload.get("source_id")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("source_id must be a non-empty string")
    return raw.strip()


def build_production_router() -> AiRouter:
    """Build the provider router from env (MOCK is forbidden in production)."""
    return AiRouter(config=AiConfig())


def _resolve_source(source_id: str) -> dict:
    """Map a registered ``sources`` row to the ingest spec (row values only)."""
    with get_session() as session:
        row = session.query(sources).filter_by(source_id=source_id).first()
    if row is None:
        raise ValueError(f"unknown source_id {source_id!r}")
    return {"source_id": row.source_id, "url": row.url or "", "type": row.type}


def _source_item_ids(source_id: str) -> list[str]:
    with get_session() as session:
        rows = session.query(source_items).filter_by(source_id=source_id).all()
    return [str(r.id) for r in rows]


def _all_item_ids() -> list[str]:
    with get_session() as session:
        rows = session.query(source_items).all()
    return [str(r.id) for r in rows]


def _outcome_view(outcome) -> dict:
    return {
        "story_id": outcome.business_key,
        "final_status": outcome.final_status,
        "error": outcome.error,
        "published": bool(outcome.publish and outcome.publish.get("published")),
    }


def run_pipeline_http(source_id: str | None) -> JSONResponse:
    """Execute (or reject) one manual pipeline run; returns a JSON response.

    Called from the admin handler inside a worker thread (run_pipeline may
    need ``asyncio`` for P1 ingestion), with auth already enforced upstream.
    """
    rid = get_request_id()
    if not _EXEC_LOCK.acquire(blocking=False):
        return JSONResponse(
            status_code=409,
            content={
                "status": "already-running",
                "detail": "a pipeline run is already in progress",
            },
        )
    try:
        source = None
        if source_id is not None:
            source = _resolve_source(source_id)

        router = build_production_router()
        route = router.route("generate")
        ai = {"provider": route.provider, "model": route.model, "mock": route.mock}
        log_event(
            logger, "pipeline_trigger_start", request_id=rid, source_id=source_id,
            ai_provider=ai["provider"], ai_model=ai["model"], ai_mock=ai["mock"],
        )

        if source_id is not None:
            ingest = asyncio.run(ingest_source(source))
            if ingest.errors:
                detail = "; ".join(ingest.errors)
                log_event(
                    logger, "pipeline_trigger_ingest_failed", request_id=rid,
                    source_id=source_id, error_message=detail[:500],
                )
                return JSONResponse(
                    status_code=500,
                    content={"status": "error", "phase": "INGEST", "detail": detail},
                )
            ids = _source_item_ids(source_id)
        else:
            ids = _all_item_ids()

        result = run_pipeline(signal_ids=ids, ai_router=router)
        outcomes = [_outcome_view(o) for o in result["outcomes"]]
        published = sum(1 for o in outcomes if o["final_status"] == "PUBLISHED")
        log_event(
            logger, "pipeline_trigger_end", request_id=rid, source_id=source_id,
            stories_detected=result["stories_detected"],
            stories_processed=result["stories_processed"], published=published,
        )
        return JSONResponse(
            content={
                "status": "ok",
                "request_id": rid,
                "source_id": source_id,
                "stories_detected": result["stories_detected"],
                "stories_processed": result["stories_processed"],
                "published": published,
                "ai": ai,
                "outcomes": outcomes,
            }
        )
    except ValueError as exc:  # source resolution (after the lock is held)
        log_event(
            logger, "pipeline_trigger_rejected", request_id=rid,
            error_message=str(exc),
        )
        return JSONResponse(status_code=400, content={"status": "error", "detail": str(exc)})
    except PipelinePhaseError as exc:
        persist_error(
            module="web.pipeline_trigger", error_type="PipelinePhaseError",
            message=str(exc.context), context={"phase": exc.phase},
        )
        log_event(
            logger, "pipeline_trigger_phase_failed", request_id=rid,
            phase=exc.phase, error_message=str(exc.context)[:500],
        )
        return JSONResponse(
            status_code=500,
            content={"status": "error", "phase": exc.phase, "detail": str(exc.context)},
        )
    except Exception as exc:  # noqa: BLE001 - surfaced explicitly, never hidden
        persist_error(
            module="web.pipeline_trigger", error_type=type(exc).__name__,
            message=str(exc), context={"request_id": rid},
        )
        log_event(
            logger, "pipeline_trigger_failed", request_id=rid,
            error_type=type(exc).__name__, error_message=str(exc)[:500],
        )
        return JSONResponse(
            status_code=500,
            content={
                "status": "error", "error_type": type(exc).__name__, "detail": str(exc),
            },
        )
    finally:
        _EXEC_LOCK.release()