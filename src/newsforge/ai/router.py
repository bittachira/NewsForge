"""P4 — AI Router + cost engine (§30-§32).

Provider-independent model router. In MOCK mode (the default) no network, provider or
credential is touched: routing returns a deterministic local model and
:meth:`AiRouter.generate` synthesises output offline. In real mode the router calls an
OpenAI-compatible ``/chat/completions`` endpoint (httpx, no streaming), with the API key
read ONLY from :class:`newsforge.config.AiConfig` (env). Every logical generation records
exactly ONE ``ai_jobs`` row plus one ``ai_runs`` link row (idempotent by ``run_id`` == the
artifact business key), so AI cost per article is auditable (§30).

Security: the API key never appears in returned dicts, persisted rows, logs or exceptions.
Latency/cost for MOCK are deterministic (§15); real-provider latency/tokens come from the
server response and may vary.
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
    # USD per 1k tokens for the selected tier (MOCK uses mock_cost_per_1k_tokens;
    # real providers use AiConfig.cost_per_1k_tokens).
    cost_per_1k_tokens: float = 0.0


class AiRouter:
    """Model router. MOCK mode is fully offline and deterministic (§15)."""

    def __init__(self, config: Optional[AiConfig] = None):
        self.config = config if config is not None else AiConfig()

    def route(self, task_type: str) -> AiRoute:
        """Select the provider/model for a task. Deterministic for fixed config."""
        if self.config.mock:
            return AiRoute(
                provider="mock",
                model="deterministic-template",
                mock=True,
                cost_per_1k_tokens=self.config.mock_cost_per_1k_tokens,
            )
        models = {
            "openai": self.config.medium_model,
            "lm_studio": self.config.small_model,
            "ollama": self.config.ollama_model,
        }
        provider = self.config.default_provider or "openai"
        return AiRoute(
            provider=provider,
            model=models.get(provider, self.config.medium_model),
            mock=False,
            cost_per_1k_tokens=self.config.cost_per_1k_tokens if provider in ("openai", "lm_studio") else 0.0,
        )

    def _base_url(self, route: AiRoute) -> str:
        if route.provider == "lm_studio":
            return self.config.lm_studio_base_url
        if route.provider == "ollama":
            return self.config.ollama_base_url
        return self.config.openai_base_url

    def _api_key(self, route: AiRoute) -> str | None:
        """Resolve the credential for a provider. Never raised/logged; caller enforces."""
        if route.provider == "lm_studio":
            return self.config.lm_studio_api_key
        return self.config.openai_api_key

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Deterministic token estimate (~4 chars/token); no randomness."""
        t = " ".join((text or "").split())
        return len(t) // 4 if t else 0

    def compute_cost(self, tokens_input: int, tokens_output: int, route: Optional[AiRoute] = None) -> float:
        """Cost from the configured per-1k-tokens rate (MOCK tier in MVP)."""
        sel = route if route is not None else self.route(AiTaskType.GENERATE.value)
        rate = sel.cost_per_1k_tokens if not sel.mock else self.config.mock_cost_per_1k_tokens
        return round((tokens_input + tokens_output) / 1000.0 * rate, 6)

    def generate(self, *, input_text: str, task_type: str = AiTaskType.GENERATE.value) -> dict:
        """Run one generation for ``input_text``.

        Returns a dict with ``text`` (the completion string), ``tokens_input``,
        ``tokens_output``, ``cost_usd``, ``model`` and ``provider``.

        MOCK mode: synthesises a deterministic completion from the input (no network).
        Real mode: calls the OpenAI-compatible chat endpoint over httpx. Raises
        :class:`ProviderError` on missing credentials, HTTP/timeout failures or a
        malformed response — the router NEVER silently falls back to MOCK, so a real
        provider failure is visible to the caller (§30).

        The API key is consumed from the config only and never included in the returned
        dict or any error message.
        """
        route = self.route(task_type)
        if route.mock:
            return {
                "text": _mock_completion(input_text),
                "tokens_input": self.estimate_tokens(input_text),
                "tokens_output": self.estimate_tokens(_mock_completion(input_text)),
                "cost_usd": self.compute_cost(
                    self.estimate_tokens(input_text),
                    self.estimate_tokens(_mock_completion(input_text)),
                    route=route,
                ),
                "model": route.model,
                "provider": route.provider,
                "mock": True,
            }

        key = self._api_key(route)
        if not key:
            raise ProviderError(
                f"provider {route.provider!r} requires an API key "
                "(set NEWSFORGE_OPENAI_API_KEY or NEWSFORGE_LM_STUDIO_API_KEY)"
            )

        import httpx

        url = f"{self._base_url(route).rstrip('/')}/chat/completions"
        body = {
            "model": route.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": input_text},
            ],
            "temperature": 0.0,
            "stream": False,
            "max_tokens": self.config.max_tokens,
        }
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            resp = httpx.post(
                url, json=body, headers=headers, timeout=self.config.request_timeout_s
            )
        except (httpx.HTTPError, OSError, ValueError, TimeoutError) as exc:  # transport/DNS/bad-URL
            raise ProviderError(
                f"provider request failed: {type(exc).__name__}: {_redact(key, exc)}"
            ) from exc
        if resp.status_code != 200:
            detail = resp.text[:200] if resp.text else "no body"
            raise ProviderError(
                f"provider returned HTTP {resp.status_code}: {_redact(key, detail)}"
            )

        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"malformed provider response: {type(exc).__name__}") from exc
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("provider returned empty completion")

        tokens_in = int(data.get("usage", {}).get("prompt_tokens") or self.estimate_tokens(input_text))
        tokens_out = int(data.get("usage", {}).get("completion_tokens") or self.estimate_tokens(text))
        return {
            "text": text,
            "tokens_input": tokens_in,
            "tokens_output": tokens_out,
            "cost_usd": self.compute_cost(tokens_in, tokens_out, route=route),
            "model": route.model,
            "provider": route.provider,
            "mock": False,
        }


_SYSTEM_PROMPT = (
    "You are an expert journalism editor. Write ONLY verified facts. "
    "Never add speculation, opinion or unsourced claims. "
    "Return plain text: a title on the first line, then a summary, then facts."
)


def _redact(secret: str | None, text) -> str:
    """Replace occurrences of a credential in free text (defensive redaction).

    If a provider/proxy ever echoed the request body in an error detail, the API key must
    never reach logs or callers — it is masked before the exception message is built."""
    s = str(text)
    if secret:
        s = s.replace(secret, "[REDACTED]")
    if "Bearer " in s:
        import re as _re

        s = _re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", s)
    return s


def _mock_completion(input_text: str) -> str:
    """Deterministic offline completion used ONLY when config.mock is True (§15)."""
    lines = [l for l in (input_text or "").splitlines() if l.strip()]
    facts = [l.split("::", 1)[-1].strip() for l in lines[1:] if "::" in l]
    title = (lines[0] if lines else "Artículo verificable").split("|", 1)[-1]
    parts = [title, "Resumen generado localmente (MOCK)."]
    parts.extend(fact for fact in facts[:5])
    return "\n".join(parts)


class ProviderError(RuntimeError):
    """A provider call failed (credentials, transport, HTTP or malformed response)."""


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
