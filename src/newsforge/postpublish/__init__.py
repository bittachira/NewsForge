"""P5 — Post-publish monitoring (section 28).

Observer-only. Reads the real P4 artifacts and captured snapshots; writes ONLY to
``published_snapshots`` / ``postpublish_events``. Never publishes, never mutates editorial tables,
never re-derives verdicts (§11/§24). Detection produces review *signals*, not bypasses.

Public API: :func:`detect_changes`, :func:`detect_stale`, :func:`mark_needing_update`,
:func:`reconstruct_provenance`.
"""
from __future__ import annotations

from .monitoring import (
    DEFAULT_STALE_POLICY,
    detect_changes,
    detect_stale,
    mark_needing_update,
    reconstruct_provenance,
)

__all__ = [
    "detect_changes",
    "detect_stale",
    "mark_needing_update",
    "reconstruct_provenance",
    "DEFAULT_STALE_POLICY",
]
