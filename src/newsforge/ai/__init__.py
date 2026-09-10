"""P4 — AI layer: model router + cost engine (MOCK is the default, fully offline).

Public API (only interfaces that actually exist in :mod:`newsforge.ai.router`):
:class:`AiRouter`, :class:`AiRoute`, :func:`record_generation_job`.
"""
from __future__ import annotations

from .router import AiRoute, AiRouter, record_generation_job

__all__ = [
    "AiRoute",
    "AiRouter",
    "record_generation_job",
]
