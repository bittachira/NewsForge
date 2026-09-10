"""Structured JSON line logging + redaction (OPS_HARDENING_OBSERVABILITY §1).

Every log line is a single JSON object written to ``sys.stdout`` (Docker-compatible:
each line parses standalone, e.g. ``docker logs | jq``). The JSON document always
carries ``timestamp`` (UTC ISO-8601), ``level``, ``component`` (the logger name),
``event`` and ``message``. Correlators — ``request_id`` (from the active context),
``run_id``, ``story_id``, ``artifact_id``, ``job_id`` — and operational fields are
included ONLY when present (optional fields are omitted, never ``null``).

Security: secrets must never be logged. ``redact_value`` masks values whose keys are
sensitive (.. tokens..), ``redact_text`` masks secret-like token bodies and every
``message``/``error_message`` passes through redaction before being serialized.

Usage::

    from newsforge.core.logger import get_logger, log_event

    logger = get_logger("newsforge.pipeline")
    log_event(logger, "phase_start", phase="INGEST", run_id="...")
    logger.warning("something happened: %s", detail)          # still structured JSON

The legacy :class:`EventLogger` (an audit-event abstraction that was never adopted and
whose docstring promised a non-existent ``event()`` helper) has been REMOVED. Business
/editorial audit persistence lives on as :func:`newsforge.verify.persist.audit_event` —
that writes to the ``audit_logs`` table and is deliberately NOT equivalent to this
console logger.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

from newsforge.core.request_context import get_request_id

# Canonical ordering of the JSON document; the request id is injected after ``event``.
_ORDER = (
    "timestamp",
    "level",
    "component",
    "event",
    "request_id",
    "run_id",
    "story_id",
    "artifact_id",
    "job_id",
    "phase",
    "result",
    "duration_ms",
    "provider",
    "model",
    "destination",
    "attempt",
    "task_type",
    "mock",
    "cost_usd",
    "tokens_input",
    "tokens_output",
    "status",
    "added",
    "skipped",
    "stories_detected",
    "stories_processed",
    "version",
    "git_commit",
    "build_time",
    "python_version",
    "schema_version",
    "error_type",
    "error_message",
    "message",
)
_EXTRA_KEYS = frozenset(_ORDER)

_MAX_ERROR_MESSAGE_LEN = 500

_SENSITIVE_VALUE_KEYS = frozenset({
    "api_key", "apikey", "token", "tokens", "access_token", "auth_token",
    "secret", "secrets", "password", "passwd", "authorization", "auth",
    "cookie", "cookies", "credential", "credentials", "client_secret",
})

_SECRET_BODY_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
    re.compile(r"\bxox[baprs]-\S+"),
    re.compile(r"Bearer\s+\S+"),
]


def redact_text(value: str) -> str:
    """Mask secret-looking token bodies inside free text (defensive redaction)."""
    s = str(value)
    for pattern in _SECRET_BODY_PATTERNS:
        s = pattern.sub("[REDACTED]", s)
    return s


def redact_value(value: Any) -> Any:
    """Recursively mask values whose keys are sensitive; redacts text bodies too."""
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if str(k).lower() in _SENSITIVE_VALUE_KEYS else redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _timestamp(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds")


def _error_fields(record: logging.LogRecord) -> dict:
    if not record.exc_info:
        return {}
    exc_value = record.exc_info[1]
    return {
        "error_type": record.exc_info[0].__name__ if record.exc_info[0] else "Exception",
        "error_message": redact_text(str(exc_value))[:_MAX_ERROR_MESSAGE_LEN],
    }


class JsonFormatter(logging.Formatter):
    """Emit one compact, deterministic JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        extras = record.__dict__
        entry: dict[str, Any] = {
            "timestamp": _timestamp(record),
            "level": record.levelname,
            "component": record.name,
            "event": extras.get("event") or "log",
        }
        request_id = extras.get("request_id") or get_request_id()
        if request_id:
            entry["request_id"] = request_id
        for key in _EXTRA_KEYS:
            if key in entry or key in ("timestamp", "level", "component", "event", "request_id"):
                continue
            if key in extras and extras[key] is not None:
                entry[key] = redact_value(extras[key])
        entry["message"] = redact_text(record.getMessage())
        entry.update(_error_fields(record))
        return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))


def _build_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured (e.g. tests)

    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger(name: str = "newsforge") -> logging.Logger:
    return _build_logger(name)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    message: str | None = None,
    **fields: Any,
) -> None:
    """Emit a structured event line with the given correlation/operational fields.

    ``fields`` must be JSON-serializable scalars (``str``/``int``/``float``/``bool``);
    only the canonical field names are serialized (see ``_ORDER``). Sensitive values
    are masked by the formatter before they reach the output.
    """
    safe = {k: v for k, v in fields.items() if k in _EXTRA_KEYS}
    logger.log(level, message or event, extra={"event": event, **safe})