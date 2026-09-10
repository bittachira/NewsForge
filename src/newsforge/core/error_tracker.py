"""Critical-error persistence to the ``errors`` table (OPS_HARDENING_OBSERVABILITY §8).

``persist_error`` records ONLY critical, unexpected operational errors (pipeline phase
failures, provider failures, …). It is BEST-EFFORT and NEVER raises: if the database is
not available (or the write fails), the error is dropped with a warning — error tracking
must never break the transaction that detected the error, and it must never take the
application down when the *tracking* storage fails.

Recurring / expected conditions are deliberately NOT persisted (they are handled and
recorded elsewhere): HTTP 404s, normal input validation, SSRF blocks, auth failures,
readiness probes, and business decisions (REJECT / WAIT / BLOCKED).

The stored message is sanitized (secret-like bodies masked) and truncated; the context
JSON is recursively redacted and carries correlation ids (``request_id``, ``run_id``,
``story_id``, …) so a stored error can be tied back to a request/pipeline run.
"""
from __future__ import annotations

import logging
from typing import Any

from newsforge.core.logger import get_logger, redact_text, redact_value
from newsforge.core.metrics import metrics
from newsforge.core.request_context import get_request_id

logger = get_logger("core.error_tracker")
_MAX_MESSAGE_LEN = 500


def sanitize_message(message: str, *, limit: int = _MAX_MESSAGE_LEN) -> str:
    """Mask secret-like bodies and truncate; always returns a string."""
    text = redact_text(str(message or ""))
    return text[:limit]


def persist_error(
    *,
    module: str,
    error_type: str,
    message: str,
    request_id: str | None = None,
    context: dict | None = None,
) -> str | None:
    """Best-effort insert of one critical error into ``errors``.

    Returns the new row id, or ``None`` when the database is not initialized or the
    write failed (never raises).
    """
    from newsforge.db.models import errors, to_jsonable
    from newsforge.db.session import get_session, is_database_initialized

    if not is_database_initialized():
        return None  # no database yet (e.g. standalone unit tests) -> nothing to write

    rid = request_id or get_request_id()
    clean_context = redact_value(dict(context or {}))
    if rid:
        clean_context["request_id"] = rid
    context_json = to_jsonable(clean_context) if clean_context else None
    try:
        with get_session() as session:
            row = errors(
                module=module,
                error_type=error_type or "Exception",
                message=sanitize_message(message),
                context_json=context_json,
            )
            session.add(row)
            session.commit()
            return str(row.id)
    except Exception as exc:  # noqa: BLE001 - tracking is best effort; NEVER propagates
        metrics().inc("db_errors_total", tags={"component": "error_tracker"})
        logger.warning(
            "error_tracker: failed to persist %s error (type=%s): %s",
            module, error_type or "?", exc,
        )
        return None