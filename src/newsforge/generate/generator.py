"""P4 — content generation engine (local + offline).

The generator emits ONLY facts backed by the supplied claims/evidence. Claims without
sufficient evidence are never stated as facts: they are excluded from the body and
recorded in ``excluded_claims`` so the exclusion is auditable. No external API, LLM
provider, credential or network access is used — output is a pure function of its inputs
and an injectable ``reference_time`` (§15 determinism)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


GENERATOR_VERSION = "p4.v1"
TEMPLATE_VERSION = "p4.v1"
MODEL_NAME = "deterministic-template"


@dataclass(frozen=True)
class GeneratedContent:
    """Structured output of one generator run for a (story, format) pair.

    ``claim_refs`` are the business claim keys used as facts; ``excluded_claims`` records
    every supplied claim that was NOT used because it lacks evidence (never fabricated)."""
    title: str
    summary: Optional[str]
    body_json: dict
    claim_refs: list = field(default_factory=list)
    excluded_claims: list = field(default_factory=list)
    deterministic: bool = True
    generator_version: str = GENERATOR_VERSION
    template_version: str = TEMPLATE_VERSION
    model_name: str = MODEL_NAME


class Generator:
    """Generator contract. Implementations must be local and offline (no network)."""

    name: str = "generator"
    version: str = GENERATOR_VERSION
    template_version: str = TEMPLATE_VERSION
    deterministic: bool = False

    def generate(self, *, story: dict, claims: list, format: str, reference_time: Optional[str] = None) -> GeneratedContent:
        """Produce :class:`GeneratedContent` for one (story, format). Must raise on failure."""
        raise NotImplementedError


class DeterministicGenerator(Generator):
    """Fully local template-driven generator.

    Output is a pure function of (story, claims, format, reference_time, versions): no clock
    reads, no randomness, no I/O, no network. Claims without evidence are excluded from the
    facts and listed in ``excluded_claims`` — never stated as facts."""

    name = "deterministic"
    deterministic = True

    def __init__(self, *, version: str = GENERATOR_VERSION, template_version: str = TEMPLATE_VERSION,
                 model_name: str = MODEL_NAME):
        self.version = version
        self.template_version = template_version
        self.model_name = model_name

    def generate(self, *, story: dict, claims: list, format: str, reference_time: Optional[str] = None) -> GeneratedContent:
        from newsforge.db.models import ArtifactFormat  # default only; assembly validates
        # Lazy import on purpose: assembly imports this module at load time.
        from .assembly import assemble_editorial_artifact

        return assemble_editorial_artifact(
            story=story,
            claims=claims,
            format=format if format is not None else ArtifactFormat.ARTICLE.value,
            reference_time=reference_time,
            generator_version=self.version,
            template_version=self.template_version,
            model_name=self.model_name,
        )
