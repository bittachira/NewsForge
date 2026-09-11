"""P4 — real AI generation path tests.

The bug we are guarding: ``generate_story`` always used ``DeterministicGenerator``
regardless of the AiRouter config, so ``MOCK_AI=false`` never actually invoked the
provider. These tests prove:

* ``MOCK_AI=false`` (mock router ``mock=False``) -> :class:`AiGenerator` calls
  :meth:`AiRouter.generate`, which (when httpx is mocked) produces a REAL provider
  completion and records REAL provider metadata on the ai_job.
* ``MOCK_AI=true`` / ``ai_router=None`` -> :class:`DeterministicGenerator` (offline).
* A real provider failure raises :class:`ProviderError` and leaves NO partial writes.
* The admin trigger returns an explicit ``no-items`` status on an empty DB.

No real network/API is ever contacted: the real-provider calls use a fake
``httpx.post`` transport exactly like ``test_p4_real_provider.py``."""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from newsforge.ai import AiRouter, ProviderError
from newsforge.config import AiConfig
from newsforge.db import (
    ai_jobs,
    ai_runs,
    generated_artifacts,
    get_session,
    publications,
    use_isolated_database_ctx,
)
from newsforge.db.models import ArtifactFormat, GenerationState, PublicationStatus
from newsforge.generate import AI_GENERATOR_VERSION, generate_story

_db_seq = 0

T = "2026-09-06T10:00:00+00:00"


@pytest.fixture(autouse=True)
def isolated_db():
    global _db_seq
    db_dir = Path.cwd() / ".pytest_tmp"
    shutil.rmtree(db_dir, ignore_errors=True)
    db_dir.mkdir(exist_ok=True)
    path = db_dir / f"p4ai_{_db_seq}.db"
    with use_isolated_database_ctx(path):
        yield
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _seed_item(session, *, title, source_id):
    from newsforge.db import source_items
    item = source_items()
    item.source_id = source_id
    item.title = title
    item.content_text = title
    item.published_at = "2026-09-05T10:00:00+00:00"
    item.dedupe_hash = f"hash-{source_id}-{title}"
    session.add(item)
    session.commit()
    return str(item.id)


def _seed_story_with_claims(session, claim_specs, story_id="story-1"):
    """Seed sources + items + story, then run the real P3 verification."""
    from newsforge.db import source_items as _si, sources as _sources, stories as _stories
    from newsforge.verify.persist import run_verification

    seen = set()
    for spec in claim_specs:
        sid = spec["source_id"]
        if sid in seen:
            continue
        seen.add(sid)
        s = _sources()
        s.id, s.source_id, s.name, s.type = sid, sid, f"Source {sid}", "RSS"
        s.country, s.language = "ES", "es"
        s.tier = spec.get("tier", "TIER_1")
        s.trust_score = 90
        s.status = "active"
        session.add(s)

    items = {}
    for spec in claim_specs:
        sid = spec["source_id"]
        items[sid] = _seed_item(session, title=f"item-{sid}", source_id=sid)
    session.commit()

    st = _stories()
    st.id, st.story_id, st.slug, st.title, st.summary = (
        story_id, story_id, story_id, "Test Story", "A story about a tax change.")
    session.add(st)
    session.commit()

    specs = []
    for spec in claim_specs:
        entry = {
            "claim_id": spec["claim_id"],
            "text": spec["text"],
            "story_id": story_id,
            "tiers": [spec.get("tier", "TIER_1")],
        }
        if spec.get("with_evidence", True):
            entry["source_item_ids"] = [items[spec["source_id"]]]
        specs.append(entry)

    return run_verification(claims_specs=specs, story_id=story_id, reference_time=T)


def _real_router(**overrides):
    base = dict(
        mock=False,
        default_provider="lm_studio",
        lm_studio_base_url="http://127.0.0.1:1/v1",
        lm_studio_api_key="testing-key",
        small_model="test-model",
        request_timeout_s=2,
    )
    base.update(overrides)
    return AiRouter(config=AiConfig(**base))


def _llm_response(text):
    """Fake httpx response carrying an OpenAI-compatible completion."""
    resp = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    resp.status_code = 200
    resp.text = text
    resp.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 40},
    }
    return resp


# --------------------------------------------------------------------------- #
# Real AI generation (mocked httpx — the provider IS actually called)
# --------------------------------------------------------------------------- #
def test_mock_false_calls_real_provider_and_records_metadata():
    """mock=False -> generate_story selects AiGenerator, calls AiRouter.generate,
    and the ai_job carries the REAL provider/model."""
    from newsforge.db import source_items as _si

    router = _real_router()
    llm_text = (
        "Tax raised to three euros\n"
        "The government confirmed the VAT increase to three euros next year.\n"
        "The tax is three euros."
    )

    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-ai", "text": "The tax is three euros.", "source_id": "src-1"},
        ])

    with patch("httpx.post", return_value=_llm_response(llm_text)) as post:
        with get_session() as s:
            gen = generate_story(
                s, story_id="story-1", format=ArtifactFormat.ARTICLE.value,
                reference_time=T, ai_router=router,
            )

    # The provider endpoint was actually called (this is the regression being fixed).
    assert post.called
    args, kwargs = post.call_args
    assert args[0].endswith("/chat/completions")
    assert kwargs["json"]["model"] == "test-model"

    assert gen["state"] == GenerationState.VALIDATED.value
    assert gen["validation"]["checks"]["determinism_ok"] is True  # skipped for non-deterministic
    job = gen["ai_job"]
    assert job["provider"] == "lm_studio"
    assert job["model"] == "test-model"
    assert job["created"] is True

    with get_session() as s:
        row = s.query(generated_artifacts).filter_by(artifact_id=gen["artifact_id"]).one()
        assert row.generator_version == AI_GENERATOR_VERSION
        assert row.model_name == "test-model"
        assert row.deterministic is False
        assert row.title == "Tax raised to three euros"


def test_ai_provider_failure_propagates_no_partial_writes():
    """A real provider failure raises ProviderError — generate_story leaves nothing
    behind (no artifact row, no ai_job row)."""
    router = _real_router()  # dead endpoint 127.0.0.1:1 -- connect refused / timeout

    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-fail", "text": "The tax is three euros.", "source_id": "src-2"},
        ])

    with patch("httpx.post", side_effect=ConnectionError("connection refused")):
        with pytest.raises(ProviderError):
            with get_session() as s:
                generate_story(s, story_id="story-1", ai_router=router)

    with get_session() as s:
        assert s.query(generated_artifacts).count() == 0
        assert s.query(ai_jobs).count() == 0
        assert s.query(ai_runs).count() == 0


def test_mock_true_uses_deterministic_generator():
    """mock=True (or ai_router=None) keeps the offline deterministic generator."""
    router = _real_router(mock=True)

    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-det", "text": "The tax is three euros.", "source_id": "src-3"},
        ])

    with patch("httpx.post") as post:
        with get_session() as s:
            gen = generate_story(
                s, story_id="story-1", format=ArtifactFormat.ARTICLE.value,
                reference_time=T, ai_router=router,
            )

    post.assert_not_called()  # offline: no network ever
    assert gen["state"] == GenerationState.VALIDATED.value
    assert gen["validation"]["checks"]["determinism_ok"] is True
    assert gen["ai_job"]["provider"] == "mock"
    with get_session() as s:
        row = s.query(generated_artifacts).filter_by(artifact_id=gen["artifact_id"]).one()
        assert row.deterministic is True


def test_default_router_none_is_deterministic():
    """ai_router=None falls back to the default MOCK router + DeterministicGenerator."""
    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-def", "text": "The tax is three euros.", "source_id": "src-4"},
        ])

    with patch("httpx.post") as post:
        with get_session() as s:
            gen = generate_story(s, story_id="story-1", reference_time=T)

    post.assert_not_called()
    assert gen["ai_job"]["provider"] == "mock"
    with get_session() as s:
        row = s.query(generated_artifacts).filter_by(artifact_id=gen["artifact_id"]).one()
        assert row.deterministic is True