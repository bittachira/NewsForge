"""Structured, audit-friendly logging for NewsForge (observability §42).

Writes timestamped lines to the console and, when enabled, to a rotating log file.
The :func:`event` helper records pipeline lifecycle events so every autonomous
action is traceable in ``audit_logs`` / logs.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def _build_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured (e.g. tests)

    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger(name: str = "newsforge") -> logging.Logger:
    return _build_logger(name)


class EventLogger:
    """Records structured pipeline events (audit trail for autonomous actions)."""

    def __init__(self, name: str = "events"):
        self._logger = _build_logger(name)

    def record(
        self,
        event: str,
        *,
        payload: dict | None = None,
        level: int = logging.INFO,
    ) -> None:
        msg = f"EVENT {event}"
        if payload:
            msg += f" :: {payload}"
        self._logger.log(level, msg)
