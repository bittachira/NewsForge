"""P5 — Measurement (section 27).

Observer-only layer. Turns the REAL P4 artifacts (:class:`publications`,
:class:`publication_attempts`) into deterministic, idempotent measurements and snapshots. It only
READS editorial tables and writes to the measurement/snapshot/event tables; it never mutates
``decisions``, ``trust_evaluations``, ``quality_evaluations``, ``articles`` or ``stories`` and never
re-derives an editorial verdict (§11).

Public API: :func:`record_publication_metrics`, :func:`record_destination_metrics`,
:func:`capture_snapshot`.
"""
from __future__ import annotations

from .metrics import (
    capture_snapshot,
    record_destination_metrics,
    record_publication_metrics,
)

__all__ = [
    "capture_snapshot",
    "record_destination_metrics",
    "record_publication_metrics",
]
