"""G1 — Independent corroboration is based on SOURCE, not source_item (adversarial Case 1).

Two DISTINCT articles that originate from the SAME source must NOT count as two independent sources.
This is verified end-to-end through the real ``run_verification`` pipeline against an isolated DB,
so it exercises :func:`_resolve_source_ids` and the full trust evaluation rather than a unit of
``independent_sources_from`` in isolation.

Also asserts the positive control: two genuinely distinct sources DO count as independent.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import newsforge.db as db
from newsforge.db import get_session, sources, source_items, stories
from newsforge.verify.persist import run_verification


@pytest.fixture(autouse=True)
def isolated_db():
    """Give every test its own throwaway database on the same drive as cwd."""
    db_dir = Path.cwd() / ".pytest_tmp"
    shutil.rmtree(db_dir, ignore_errors=True)
    db_dir.mkdir(exist_ok=True)
    path = db_dir / "test.db"
    with db.use_isolated_database_ctx(path):
        yield
    shutil.rmtree(db_dir, ignore_errors=True)


def _seed_source(session, *, id, name, tier):
    src = sources()
    src.id = id
    src.source_id = id
    src.name = name
    src.type = "RSS"
    src.country = "ES"
    src.language = "es"
    src.tier = tier
    src.trust_score = 90
    src.status = "active"
    session.add(src)
    session.commit()


def _seed_item(session, *, title, source_id, published_at="2026-09-05T10:00:00+00:00"):
    item = source_items()
    item.source_id = source_id
    item.title = title
    item.description = None
    item.content_html = None
    item.content_text = title
    item.published_at = published_at
    item.dedupe_hash = f"hash-{title}"
    session.add(item)
    session.commit()
    return str(item.id)


def _seed_story(session, story_id="my-story-1"):
    st = stories()
    st.story_id = story_id
    st.slug = story_id
    st.title = "My Story"
    session.add(st)
    session.commit()
    return story_id


def _run(claims_specs, *, story_id="my-story-1", reference_time="2026-09-06T10:00:00+00:00"):
    return run_verification(claims_specs=claims_specs, story_id=story_id, reference_time=reference_time)


def _first_trust_eval(result):
    return result["trust_evaluations"][0]


# --------------------------------------------------------------------------- #
# Case 1 (adversarial): two articles from the SAME source are NOT independent.
# --------------------------------------------------------------------------- #
def test_two_articles_same_source_are_one_independent_source():
    """Source A -> Article 1 + Article 2 must collapse to independent_sources == 1."""
    with get_session() as s:
        _seed_source(s, id="src-A", name="Reuters-like", tier="TIER_1")
        item_a = _seed_item(s, title="Article One", source_id="src-A")
        item_b = _seed_item(s, title="Article Two", source_id="src-A")

    claim_specs = [
        {
            "claim_id": "c-same-source",
            "text": "The tax enters into force on 1 November 2026.",
            "story_id": "my-story-1",
            "source_item_ids": [item_a, item_b],
            "tiers": ["TIER_1", "TIER_1"],
        },
    ]

    result = _run(claim_specs)
    evals = {e["target_id"]: e for e in result["trust_evaluations"]}
    assert evals["c-same-source"]["independent_corroboration"] == 1, (
        "two articles from the same source must NOT count as two independent sources"
    )


def test_two_distinct_sources_are_independent():
    """Positive control: Source A + Source B genuinely corroborate independently."""
    with get_session() as s:
        _seed_source(s, id="src-A", name="Reuters-like", tier="TIER_1")
        item_a = _seed_item(s, title="Article One", source_id="src-A")
        _seed_source(s, id="src-B", name="AP-like", tier="TIER_1")
        item_b = _seed_item(s, title="Article Two", source_id="src-B")

    claim_specs = [
        {
            "claim_id": "c-distinct-sources",
            "text": "The tax enters into force on 1 November 2026.",
            "story_id": "my-story-1",
            "source_item_ids": [item_a, item_b],
            "tiers": ["TIER_1", "TIER_1"],
        },
    ]

    result = _run(claim_specs)
    evals = {e["target_id"]: e for e in result["trust_evaluations"]}
    assert evals["c-distinct-sources"]["independent_corroboration"] == 2, (
        "two genuinely distinct sources SHOULD count as two independent sources"
    )


def test_same_source_lower_trust_than_distinct_sources():
    """The trust score must be lower when the same source repeats than when sources differ."""
    with get_session() as s:
        _seed_source(s, id="src-A", name="Reuters-like", tier="TIER_1")
        item_a = _seed_item(s, title="Article One", source_id="src-A")
        item_b = _seed_item(s, title="Article Two", source_id="src-A")

    same_source = _run(claims_specs=[{
        "claim_id": "c-same", "text": "The tax enters into force on 1 November 2026.",
        "story_id": "my-story-1", "source_item_ids": [item_a, item_b], "tiers": ["TIER_1", "TIER_1"],
    }])

    with get_session() as s:
        _seed_source(s, id="src-B", name="AP-like", tier="TIER_1")
        item_c = _seed_item(s, title="Article Three", source_id="src-B")

    distinct_sources = _run(claims_specs=[{
        "claim_id": "c-distinct", "text": "The tax enters into force on 1 November 2026.",
        "story_id": "my-story-1", "source_item_ids": [item_a, item_c], "tiers": ["TIER_1", "TIER_1"],
    }])

    assert same_source["trust_evaluations"][0]["trust_score"] < distinct_sources["trust_evaluations"][0]["trust_score"]

