"""Process-local request correlation (OPS_HARDENING_OBSERVABILITY §2).

A single ``contextvars.ContextVar`` carries the active ``request_id`` for the current
HTTP request (or pipeline run). The structured JSON logger reads it automatically, so
every log line emitted inside a request is correlated without threading it through
every call. There is exactly one source of truth for the running request id: the
middleware (``newsforge.web.middleware``) sets it per request and resets it afterwards.
"""
from __future__ import annotations

import contextvars

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)


def get_request_id() -> str | None:
    """Return the current request id, or ``None`` outside any request."""
    return request_id_var.get()