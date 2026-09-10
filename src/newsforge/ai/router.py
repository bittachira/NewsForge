"""P4 — MOCK AI Router + cost engine (§30-§32).

Provider-independent model router. In MOCK mode (the default) no network, provider or
credential is touched: routing returns a deterministic local model, token usage is
deterministically estimated from text length and cost comes from the configured per-1k
tokens rate. Every logical generation records exactly ONE ``ai_jobs`` row plus one
``ai_runs`` link row (idempotent by ``run_id`` == the artifact business key), so AI cost
per article is auditable (§30). All of this is a pure function of its inputs — no clock
reads, no randomness (§15 determinism; latency is the configured constant, not measured).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from newsforge.config import AiConfig
from newsforge.db.models import AiJobStatus, AiTaskType, ai_jobs, ai_runs


@dataclass(frozen=True)
class AiRoute:
    """Result of routing one task to a provider/model."""

    provider: str
    model: str
    mock: bool


class AiRouter:
    """Model router. MOCK mode is fully offline and deterministic (§15)."""

    def __init__(self, config: Optional[AiConfig] = None):
        self.config = config if config is not None else AiConfig()

    def route(self, task_type: str) -> AiRoute:
        """Select the provider/model for a task. Deterministic for fixed config."""
        if self.config.mock:
            return AiRoute(provider="mock", model="deterministic-template", mock=True)
        # Non-mock selection is provider-agnostic (MVP ships MOCK only; no network here).
        models = {
            "openai": self.config.medium_model,
            "lm_studio": self.config.small_model,
            "ollama": self.config.ollama_model,
        }
        provider = self.config.default_provider or "openai"
        return AiRoute(provider=provider, model=models.get(provider, self.config.medium_model), mock=False)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Deterministic token estimate (~4 chars/token); no randomness."""
        t = " ".join((text or "").split())
        return len(t) // 4 if t else 0

    def compute_cost(self, tokens_input: int, tokens_output: int) -> float:
        """Cost from the configured per-1k-tokens rate (MOCK tier in MVP)."""
        if self.config.mock:
            rate = self.config.mock_cost_per_1k_tokens
        else:
            rate = 0.0  # real provider rates are out of MOCK scope
        return round((tokens_input + tokens_output) / 1000.0 * rate, 6)


def record_generation_job(session, *, router: AiRouter, artifact_id: str,
                          input_text: str, output_text: str) -> dict:
    """Record exactly ONE ``ai_jobs`` row (+ ``ai_runs`` link) for a logical generation.

    Idempotent by ``ai_runs.run_id`` == the artifact's business key: re-running the same
    (story, format, versions) never double-counts cost. This function only adds rows —
    the caller commits, preserving :func:`generate_story`'s single-commit contract.

    Returns a dict with ``job_id``, ``run_id`` (== artifact_id), ``created``,
    ``provider``, ``model``, ``tokens_input``, ``tokens_output`` and ``cost_usd``.
    """
    route = router.route(AiTaskType.GENERATE.value)
    tokens_in = router.estimate_tokens(input_text)
    tokens_out = router.estimate_tokens(output_text)
    cost = router.compute_cost(tokens_in, tokens_out)
    prompt_hash = hashlib.sha256((input_text or "").encode("utf-8")).hexdigest()

    existing_run = session.query(ai_runs).filter_by(run_id=str(artifact_id)).first()
    if existing_run is not None:
        job = session.get(ai_jobs, str(existing_run.job_id)) if existing_run.job_id else None
        return {
            "job_id": str(job.id) if job is not None else None,
            "run_id": str(artifact_id),
            "created": False,
            "provider": route.provider,
            "model": route.model,
            "tokens_input": tokens_in,
            "tokens_output": tokens_out,
            "cost_usd": cost,
        }

    job = ai_jobs(
        task_type=AiTaskType.GENERATE.value,
        model_provider=route.provider,
        model_name=route.model,
        tokens_input=tokens_in,
        tokens_output=tokens_out,
        latency_ms=float(router.config.mock_latency_ms if route.mock else 0.0),
        cost_usd=cost,
        status=AiJobStatus.SUCCESS.value,
    )
    session.add(job)
    session.flush()  # obtain job.id before linking the run
    session.add(ai_runs(job_id=str(job.id), run_id=str(artifact_id), prompt_hash=prompt_hash))
    return {
        "job_id": str(job.id),
        "run_id": str(artifact_id),
        "created": True,
        "provider": route.provider,
        "model": route.model,
        "tokens_input": tokens_in,
        "tokens_output": tokens_out,
        "cost_usd": cost,
    }
