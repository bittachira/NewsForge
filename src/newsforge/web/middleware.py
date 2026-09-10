"""HTTP request-ID correlation + operational metrics (OPS_HARDENING_OBSERVABILITY §2, §5).

One middleware does two things (no duplicate per-request handling elsewhere):

* **Request ID** — reads ``X-Request-ID``; accepts ONLY valid values <=128 chars
  (``[A-Za-z0-9._:/@~-]``, no control characters); generates a random-ID when the
  header is absent/invalid/oversized; echoes the SAME id back on ``X-Request-ID`` of
  the response; sets the process-level ``ContextVar`` so every structured log line
  inside the request is correlated; resets it when the request finishes.
* **HTTP metrics** — records ``http_requests_total`` and ``http_request_duration_ms``
  with bounded-cardinality tags (method + normalized path + status). The request id is
  NEVER a metric tag.

No request *access logging* is added here: Uvicorn already logs requests and this
middleware would only duplicate it (see phase spec §12).
"""
from __future__ import annotations

import re
import time
import uuid

from newsforge.core import request_context
from newsforge.core.metrics import metrics

_MAX_REQUEST_ID_LEN = 128
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@~-]{0,127}$")

_STATIC_SEGMENTS = frozenset({
    "health", "live", "ready", "metrics", "analytics", "articles",
    "sitemap.xml", "feed.xml",
})


def valid_request_id(value: str | None) -> bool:
    """A request id is valid only when it is a short, safe printable token."""
    if value is None:
        return False
    if isinstance(value, str) and 1 <= len(value) <= _MAX_REQUEST_ID_LEN:
        return _REQUEST_ID_PATTERN.match(value) is not None
    return False


def normalize_path(path: str) -> str:
    """Map a raw URL path to a bounded-cardinality label for metric tags.

    Dynamic segments (e.g. article slugs) collapse to ``{slug}``; anything not clearly
    one of the known static routes collapses to ``{other}`` so arbitrary 404 paths can
    never explode metric cardinality.
    """
    segments = [s for s in (path or "/").split("/") if s]
    if not segments:
        return "/"
    if segments[0] == "articles":
        if len(segments) == 2:
            return "/articles/{slug}"
        return "/{other}"
    if all(s in _STATIC_SEGMENTS for s in segments):
        return "/" + "/".join(segments)
    return "/{other}"


async def request_id_middleware(request, call_next):
    """Starlette/FastAPI ``@app.middleware("http")`` handler (request_id + metrics)."""
    provided = request.headers.get("X-Request-ID")
    rid = provided if valid_request_id(provided) else uuid.uuid4().hex
    token = request_context.request_id_var.set(rid)
    request.scope["request_id"] = rid

    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers.setdefault("X-Request-ID", rid)
        return response
    finally:
        request_context.request_id_var.reset(token)
        path = normalize_path(request.url.path)
        method = request.method or "GET"
        duration_ms = round((time.perf_counter() - start) * 1000.0, 1)
        metrics().inc(
            "http_requests_total",
            tags={"method": method, "path": path, "status": str(status)},
        )
        metrics().observe(
            "http_request_duration_ms",
            value=duration_ms,
            tags={"method": method, "path": path},
        )


def install_request_id_middleware(app) -> None:
    """Attach the request-id/metrics middleware to a FastAPI/Starlette app."""
    app.middleware("http")(request_id_middleware)