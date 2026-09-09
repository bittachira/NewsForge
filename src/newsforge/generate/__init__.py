"""P6 — deterministic editorial generation (evidence-bound, offline).

The generation layer READS editorial state (stories/claims/evidence/decisions) and writes only
to ``generated_artifacts``. It never mutates ``decisions``, ``trust_evaluations``,
``quality_evaluations``, ``articles`` or ``stories``, never calls the publisher, and never turns
a WAIT/REVIEW/REJECT decision into a publication (§11). ``publishable`` on an artifact is a
derived property only; the authoritative gate stays :func:`newsforge.verify.persist.is_auto_publishable`.

Public API: :class:`Generator`, :class:`DeterministicGenerator`, :class:`GeneratedContent`,
:func:`generate_story`, :func:`assemble_editorial_artifact`, :func:`validate_generated_artifact`,
:func:`reconstruct_generation_provenance`, :func:`derive_artifact_id`.
"""
from __future__ import annotations

from .generator import (
    GENERATOR_VERSION,
    MODEL_NAME,
    TEMPLATE_VERSION,
    DeterministicGenerator,
    GeneratedContent,
    Generator,
)
from .assembly import (
    assemble_editorial_artifact,
    derive_artifact_id,
    generate_story,
    reconstruct_generation_provenance,
    validate_generated_artifact,
)

__all__ = [
    "GENERATOR_VERSION",
    "TEMPLATE_VERSION",
    "MODEL_NAME",
    "Generator",
    "DeterministicGenerator",
    "GeneratedContent",
    "assemble_editorial_artifact",
    "derive_artifact_id",
    "generate_story",
    "validate_generated_artifact",
    "reconstruct_generation_provenance",
]
