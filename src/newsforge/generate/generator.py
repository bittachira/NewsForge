"""P4 — content generation engine (local + AI-backed).

The generator emits ONLY facts backed by the supplied claims/evidence. Claims without
sufficient evidence are never stated as facts: they are excluded from the body and
recorded in ``excluded_claims`` so the exclusion is auditable.

Two implementations:
* :class:`DeterministicGenerator` — fully local, offline, pure function of inputs (§15
  determinism). Used in MOCK/test mode.
* :class:`AiGenerator` — delegates to :class:`newsforge.ai.router.AiRouter` for real
  or mock LLM generation. When the router is configured with ``mock=False``, the
  provider is called over the network and failures propagate as explicit errors (no
  silent fallback to MOCK).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from newsforge.ai.router import AiRouter


GENERATOR_VERSION = "p4.v1"
AI_GENERATOR_VERSION = "p4.v1-ai"
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


class AiGenerator(Generator):
    """AI-backed generator that delegates to :class:`newsforge.ai.router.AiRouter`.

    Calls the configured provider (OpenAI-compatible endpoint, LM Studio, Ollama)
    via :meth:`AiRouter.generate`. When ``mock=False`` on the router, the real
    provider is called and failures propagate as :class:`newsforge.ai.router.ProviderError`
    — there is NEVER a silent fallback to MOCK.

    The LLM output is parsed into the standard :class:`GeneratedContent` shape:
    title, summary, sections with claim references. Claims that the LLM does not
    reference end up in ``excluded_claims`` so the exclusion is auditable.

    ``deterministic=False`` tells the validation layer to skip the determinism
    re-generation check (§15): identical inputs may produce different LLM output."""

    name = "ai"
    deterministic = False

    def __init__(self, *, router: "AiRouter",
                 version: str = AI_GENERATOR_VERSION,
                 template_version: str = TEMPLATE_VERSION):
        self.router = router
        self.version = version
        self.template_version = template_version

    def generate(self, *, story: dict, claims: list, format: str,
                 reference_time: Optional[str] = None) -> GeneratedContent:
        from newsforge.db.models import ArtifactFormat
        from .assembly import _ai_input_text, _parse_ai_output

        fmt = format or ArtifactFormat.ARTICLE.value
        input_text = _ai_input_text(story, claims, fmt)
        result = self.router.generate(input_text=input_text)

        return _parse_ai_output(
            text=result["text"],
            story=story,
            claims=claims,
            format=fmt,
            reference_time=reference_time,
            generator_version=self.version,
            template_version=self.template_version,
            model_name=result["model"],
        )
