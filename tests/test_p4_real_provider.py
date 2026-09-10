"""P4 — real OpenAI-compatible provider adapter tests.

The non-mock AiRouter path calls ``httpx.post`` against an OpenAI-compatible
``/chat/completions`` endpoint. These tests drive that path with a FAKE transport
(patch ``httpx.post``) so NO network or external API is ever contacted: credentials are
injected through AiConfig (env-backed in production), timeouts/HTTP errors are raised by
the fake client. Security topology:

* The API key is consumed ONLY from the config and never appears in returned dicts,
  persisted rows, exceptions or the router's logs.
* A real-provider failure (credentials, transport, HTTP, malformed response) raises
  :class:`ProviderError` — the router NEVER falls back to MOCK silently.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from newsforge.ai import AiRouter, ProviderError
from newsforge.config import AiConfig


def _cfg(**overrides):
    base = dict(
        mock=False,
        default_provider="openai",
        openai_api_key="test-secret-key-abc123",
        medium_model="gpt-4o",
        cost_per_1k_tokens=2.0,
    )
    base.update(overrides)
    return AiConfig(**base)


def _route(router):
    return router.route("GENERATE")


def _ok_response(text="Proxy: resolved.", usage_in=110, usage_out=40):
    """A valid OpenAI chat-completions fake response."""
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": usage_in, "completion_tokens": usage_out},
    }


# --------------------------------------------------------------------------- #
# Routing / cost model (real tier)
# --------------------------------------------------------------------------- #
def test_route_real_provider_config():
    router = AiRouter(config=_cfg())
    r = _route(router)
    assert r.mock is False
    assert r.provider == "openai"
    assert r.model == "gpt-4o"
    assert r.cost_per_1k_tokens == 2.0


def test_route_lm_studio_provider():
    router = AiRouter(config=_cfg(default_provider="lm_studio", lm_studio_api_key="ls-key"))
    r = _route(router)
    assert r.provider == "lm_studio"
    assert r.mock is False


def test_compute_cost_uses_real_rate():
    router = AiRouter(config=_cfg())
    cost = router.compute_cost(1000, 1000, route=_route(router))
    assert cost == pytest.approx(4.0)  # (2000 / 1000) * 2.0


# --------------------------------------------------------------------------- #
# Real generate() — success path (fake transport, no network)
# --------------------------------------------------------------------------- #
def _resp(*, status_code=200, text="ok", payload=None, json_error=None):
    """A fake httpx response (MagicMock so ``resp.json()`` stays unbound-args)."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_error is not None:
        resp.json.side_effect = json_error
    else:
        resp.json.return_value = payload if payload is not None else _ok_response()
    return resp


def test_generate_real_success_no_network():
    router = AiRouter(config=_cfg())
    resp = _resp(payload=_ok_response())
    with patch("httpx.post", return_value=resp) as post:
        out = router.generate(input_text="Fact: the tax is three euros.")
    assert post.called
    # Request went to the OpenAI-compatible endpoint with the credential on the wire.
    args, kwargs = post.call_args
    url = args[0]
    assert url == "https://api.openai.com/v1/chat/completions"
    assert kwargs["json"]["model"] == "gpt-4o"
    assert kwargs["headers"]["Authorization"] == "Bearer test-secret-key-abc123"
    assert kwargs["timeout"] is not None

    assert out["mock"] is False
    assert out["provider"] == "openai"
    assert out["model"] == "gpt-4o"
    assert out["text"] == "Proxy: resolved."
    assert out["tokens_input"] == 110 and out["tokens_output"] == 40
    assert out["cost_usd"] == pytest.approx((110 + 40) / 1000.0 * 2.0)
    # The key is NOT echoed back in the result.
    assert "test-secret-key-abc123" not in str(out)


# --------------------------------------------------------------------------- #
# Real generate() — failure paths elevate to ProviderError (no silent MOCK fallback)
# --------------------------------------------------------------------------- #
def test_generate_raises_without_api_key():
    router = AiRouter(config=_cfg(openai_api_key=None))
    with pytest.raises(ProviderError, match="API key"):
        router.generate(input_text="x")


def test_generate_raises_on_http_error():
    router = AiRouter(config=_cfg())
    resp = _resp(status_code=500, text="boom")
    with patch("httpx.post", return_value=resp):
        with pytest.raises(ProviderError, match="HTTP 500"):
            router.generate(input_text="x")


def test_generate_raises_on_timeout():
    router = AiRouter(config=_cfg())
    with patch("httpx.post", side_effect=TimeoutError("timed out")):
        with pytest.raises(ProviderError, match="timed out"):
            router.generate(input_text="x")


def test_generate_raises_on_transport_error():
    router = AiRouter(config=_cfg())
    with patch("httpx.post", side_effect=ConnectionError("refused")):
        with pytest.raises(ProviderError, match="refused"):
            router.generate(input_text="x")


def test_generate_raises_on_malformed_response():
    router = AiRouter(config=_cfg())
    resp = _resp(json_error=ValueError("no json"))
    with patch("httpx.post", return_value=resp):
        with pytest.raises(ProviderError, match="malformed"):
            router.generate(input_text="x")


def test_generate_raises_on_missing_choices():
    router = AiRouter(config=_cfg())
    resp = _resp(payload={})
    with patch("httpx.post", return_value=resp):
        with pytest.raises(ProviderError):  # KeyError inside -> malformed -> ProviderError
            router.generate(input_text="x")


def test_generate_raises_on_empty_completion():
    router = AiRouter(config=_cfg())
    resp = _resp(payload=_ok_response(text="   "))
    with patch("httpx.post", return_value=resp):
        with pytest.raises(ProviderError, match="empty"):
            router.generate(input_text="x")


# --------------------------------------------------------------------------- #
# MOCK generate() stays fully offline
# --------------------------------------------------------------------------- #
def test_generate_mock_is_offline_and_no_network():
    router = AiRouter()  # AiConfig default -> mock=True
    with patch("httpx.post") as post:
        out = router.generate(input_text="Fact: the tax is three euros.")
    post.assert_not_called()
    assert out["mock"] is True
    assert out["provider"] == "mock"
    assert out["text"]
    assert out["tokens_input"] > 0 and out["tokens_output"] > 0


# --------------------------------------------------------------------------- #
# Security: the API key must NEVER leak into results, errors or persisted rows
# --------------------------------------------------------------------------- #
def test_api_key_never_leaks_in_results_or_errors():
    secret = "super-secret-key-xyz-987"
    router = AiRouter(config=_cfg(openai_api_key=secret))
    resp = _resp(payload=_ok_response())
    with patch("httpx.post", return_value=resp):
        out = router.generate(input_text="Fact: safe.", )

    assert secret not in str(out), "returned dict must not contain the key"

    with patch("httpx.post", return_value=_resp(status_code=500, text=secret)):
        with pytest.raises(ProviderError) as exc:
            router.generate(input_text="y")
    assert secret not in str(exc.value), "exception message must not contain the key"


def test_record_generation_job_never_persists_api_key():
    """ai_jobs / ai_runs rows + the record dict carry provider identity but no credentials."""
    import json
    from pathlib import Path

    import newsforge.db as db
    from newsforge.ai.router import AiRouter, record_generation_job

    secret = "super-secret-key-xyz-987"
    router = AiRouter(config=_cfg(openai_api_key=secret, mock=False))

    db_path = Path.cwd() / ".pytest_tmp" / "test_ai_key_not_persisted.db"
    try:
        db_path.unlink()
    except OSError:
        pass
    with db.use_isolated_database_ctx(db_path):
        with db.get_session() as s:
            rec = record_generation_job(
                s, router=router, artifact_id="art-1",
                input_text="Fact: a.", output_text="Text.",
            )
            rows_json = json.dumps(rec, default=str)
            assert secret not in rows_json, "record dict must not leak the key"
            job = s.query(db.ai_jobs).filter_by(id=str(rec["job_id"])).one()
            run = s.query(db.ai_runs).filter_by(run_id="art-1").one()
            for obj in (job, run):
                assert secret not in json.dumps(
                    {c.name: getattr(obj, c.name) for c in obj.__table__.columns}, default=str
                ), "persisted AI rows must not contain the API key"
            assert str(job.model_provider) == "openai"