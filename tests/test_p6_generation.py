"""P6 — deterministic editorial generation tests (evidence-bound, offline).

Every test runs against a throwaway SQLite database (isolated per test) with its own session. No
row leaks between tests and no `.db` file is reused (§15 isolation). The generator is fully local:
no network, no LLM provider, no credentials (§15 determinism). Generation only READS editorial
state and writes to ``generated_artifacts``; it never publishes, never mutates
decisions/trust/quality/articles/stories, and a failing generator leaves no partial write (§11).

Run: python -m pytest tests/test_p6_generation.py -q
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from newsforge.db import (
    claim_evidence,
    claims,
    decisions,
    generated_artifacts,
    get_session,
    publication_attempts,
    publications,
    published_snapshots,
    quality_evaluations,
    stories,
    trust_evaluations,
    use_isolated_database_ctx,
)
from newsforge.db.models import ArtifactFormat, GenerationState, from_jsonable
from newsforge.generate import (
    GENERATOR_VERSION,
    TEMPLATE_VERSION,
    DeterministicGenerator,
    assemble_editorial_artifact,
    generate_story,
    reconstruct_generation_provenance,
    validate_generated_artifact,
)
from newsforge.measurement import capture_snapshot, record_publication_metrics
from newsforge.publish import publish_story, register_builtin_destinations, reset_registry
from newsforge.verify.persist import run_verification


# --------------------------------------------------------------------------- #
# Isolation fixtures + seeding helpers (consistent with P3/P4/P5 tests)
# --------------------------------------------------------------------------- #
_db_seq = 0

T = "2026-09-06T10:00:00+00:00"          # injected clock for reproducible runs
T2 = "2027-01-01T00:00:00+00:00"         # a second, distinct injected clock


@pytest.fixture(autouse=True)
def isolated_db():
    """Give every test its own throwaway database so no row can leak between tests.

    Files are named ``p6_{seq}.db`` — module-unique, never colliding with other test
    modules' ``tests_{seq}.db`` names (all of them share that stem under
    ``tests/__init__.py``). A stale file that cannot be removed (locked) is never reused:
    the sequence is bumped and a fresh name is picked instead.
    """
    global _db_seq
    db_dir = Path.cwd() / ".pytest_tmp"
    # Clean slate: remove any leftover files from previous modules (e.g. P5's tests_*.db)
    # before starting — guarantees no stale shared-namespace DB reaches this module or later
    # ones, which would otherwise be reused (P4's fixture does not unlink first) and cause
    # cross-module SQLite file contamination.
    shutil.rmtree(db_dir, ignore_errors=True)
    db_dir.mkdir(exist_ok=True)
    path = db_dir / f"p6_{_db_seq}.db"
    try:
        if path.exists():
            path.unlink()
    except OSError:
        _db_seq += 1
        path = db_dir / f"p6_{_db_seq}.db"
    with use_isolated_database_ctx(path):
        yield
    # Best-effort cleanup: the isolation context already disposed its engine, so the file
    # should be free. Ignore OSError (antivirus/Windows lock) in teardown — never block
    # on it, and never leak a stale file into another module's run.
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def clean_registry():
    """Start every test from a pristine destination registry (built-ins only)."""
    reset_registry()
    register_builtin_destinations()
    yield


# --------------------------------------------------------------------------- #
# Seeding helpers — the REAL P3 flow (run_verification), never invented decisions
# --------------------------------------------------------------------------- #
def _seed_item(session, *, title, source_id, published_at="2026-09-05T10:00:00+00:00"):
    from newsforge.db import source_items

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


def _seed_story(session, story_id="story-1", title="Test Story", summary="A story about a tax change."):
    st = stories()
    st.id = story_id
    st.story_id = story_id
    st.slug = story_id
    st.title = title
    st.summary = summary
    session.add(st)
    session.commit()
    return story_id


def _seed_story_with_claims(session, *, story_id="story-1", claim_specs=None):
    """Seed source(s) + item(s) + story and run the REAL P3 verification pipeline."""
    from newsforge.db import sources as _sources

    seen_sources = set()
    for spec in claim_specs:
        sid = spec["source_id"]
        if sid in seen_sources:
            continue
        seen_sources.add(sid)
        s = _sources()
        s.id = sid
        s.source_id = sid
        s.name = f"Source {sid}"
        s.type = "RSS"
        s.country = "ES"
        s.language = "es"
        s.tier = spec.get("tier", "TIER_1")
        s.trust_score = 90 if spec.get("tier", "TIER_1") == "TIER_1" else 35
        s.status = "active"
        session.add(s)

    items = {}
    for spec in claim_specs:
        sid = spec["source_id"]
        item = _seed_item(session, title=f"item-{sid}", source_id=sid)
        items[sid] = item
    session.commit()

    _seed_story(session, story_id=story_id)

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

    result = run_verification(claims_specs=specs, story_id=story_id, reference_time=T)
    return result


def _decision_snapshot(session, story_id="story-1"):
    d = session.query(decisions).filter_by(target_type="STORY", target_id=story_id).one()
    return (d.decision, d.risk_level, d.trust_score, d.reasons_json,
            bool(d.human_override), d.policy_version, d.created_at, d.updated_at)


def _eval_dumps(session):
    t = sorted(
        (str(r.id), r.target_type, r.target_id, r.trust_score, r.factors_json, r.policy_version, r.computed_at)
        for r in session.query(trust_evaluations).all()
    )
    q = sorted(
        (str(r.id), r.target_type, r.target_id, bool(r.passed), r.score, r.reasons_json, r.policy_version, r.computed_at)
        for r in session.query(quality_evaluations).all()
    )
    return t, q


def _body_text(session, artifact_id):
    row = session.query(generated_artifacts).filter_by(artifact_id=artifact_id).one()
    body = from_jsonable(row.body_json) or {}
    return " ".join(str(s.get("text", "")) for s in (body.get("sections") or []))


# --------------------------------------------------------------------------- #
# 1. basic generation from a real P3-verified story
# --------------------------------------------------------------------------- #
def test_basic_generation_from_story():
    with get_session() as s:
        result = _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-main", "text": "The tax is three euros.", "source_id": "src-1"},
        ])
    assert result["decision"] == "PUBLISH"

    with get_session() as s:
        gen = generate_story(s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T)

    assert gen["created"] is True
    assert gen["state"] == GenerationState.VALIDATED.value
    assert gen["publishable"] is True
    with get_session() as s:
        row = s.query(generated_artifacts).filter_by(artifact_id=gen["artifact_id"]).one()
        assert row.story_id == "story-1"
        assert row.title and row.title.strip()
        body = from_jsonable(row.body_json)
        assert isinstance(body, dict) and len(body["sections"]) >= 2
        assert from_jsonable(row.claim_refs_json) == ["c-main"]


# --------------------------------------------------------------------------- #
# 2. idempotent generation (same logical key -> same row)
# --------------------------------------------------------------------------- #
def test_generation_is_idempotent():
    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-idem", "text": "The tax is three euros.", "source_id": "src-2"},
        ])

    gens = []
    for _ in range(2):
        with get_session() as s:
            gens.append(generate_story(s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T))

    assert gens[0]["artifact_id"] == gens[1]["artifact_id"]
    assert gens[0]["created"] is True and gens[1]["created"] is False
    with get_session() as s:
        assert s.query(generated_artifacts).count() == 1


# --------------------------------------------------------------------------- #
# 3. every ArtifactFormat generates its own artifact
# --------------------------------------------------------------------------- #
def test_all_formats_generate():
    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-fmt", "text": "The tax is three euros.", "source_id": "src-3"},
        ])

    ids = []
    for fmt in list(ArtifactFormat):
        with get_session() as s:
            gen = generate_story(s, story_id="story-1", format=fmt.value, reference_time=T)
        assert gen["state"] == GenerationState.VALIDATED.value
        ids.append(gen["artifact_id"])

    assert len(ids) == len(ArtifactFormat)
    assert len(set(ids)) == len(ids), "each format must map to a distinct artifact_id"
    with get_session() as s:
        rows = s.query(generated_artifacts).all()
        assert len(rows) == len(ArtifactFormat)
        for row in rows:
            body = from_jsonable(row.body_json)
            assert body["format"] == row.format
            assert len(body["sections"]) >= 1


# --------------------------------------------------------------------------- #
# 4. a different generator version -> a different deterministic artifact_id
# --------------------------------------------------------------------------- #
def test_generator_version_changes_artifact():
    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-ver", "text": "The tax is three euros.", "source_id": "src-4"},
        ])

    with get_session() as s:
        gen_v1 = generate_story(s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T)
    with get_session() as s:
        gen_v2 = generate_story(
            s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T,
            generator=DeterministicGenerator(version="p6.v2"),
        )

    assert gen_v1["artifact_id"] != gen_v2["artifact_id"]
    with get_session() as s:
        r1 = s.query(generated_artifacts).filter_by(artifact_id=gen_v1["artifact_id"]).one()
        r2 = s.query(generated_artifacts).filter_by(artifact_id=gen_v2["artifact_id"]).one()
        assert r1.generator_version == GENERATOR_VERSION
        assert r2.generator_version == "p6.v2"


# --------------------------------------------------------------------------- #
# 5. a different template version -> a different deterministic artifact_id
# --------------------------------------------------------------------------- #
def test_template_version_changes_artifact():
    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-tpl", "text": "The tax is three euros.", "source_id": "src-5"},
        ])

    with get_session() as s:
        gen_t1 = generate_story(s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T)
    with get_session() as s:
        gen_t2 = generate_story(
            s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T,
            generator=DeterministicGenerator(template_version="p6.tpl2"),
        )

    assert gen_t1["artifact_id"] != gen_t2["artifact_id"]
    with get_session() as s:
        r1 = s.query(generated_artifacts).filter_by(artifact_id=gen_t1["artifact_id"]).one()
        r2 = s.query(generated_artifacts).filter_by(artifact_id=gen_t2["artifact_id"]).one()
        assert r1.template_version == TEMPLATE_VERSION
        assert r2.template_version == "p6.tpl2"


# --------------------------------------------------------------------------- #
# 6. full provenance chain: Story -> Claim -> Evidence/Source Item -> Decision -> Artifact
# --------------------------------------------------------------------------- #
def test_full_provenance_chain():
    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-prov", "text": "The tax is three euros.", "source_id": "src-6"},
        ])
        item = str(s.query(claim_evidence).first().source_item_id)

    with get_session() as s:
        gen = generate_story(s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T)

    with get_session() as s:
        prov = reconstruct_generation_provenance(s, artifact_id=gen["artifact_id"])
    assert prov["chain_complete"] is True
    assert prov["story"]["story_id"] == "story-1"
    assert prov["decision"]["decision"] == "PUBLISH"
    c0 = prov["claims"][0]
    assert c0["claim_id_key"] == "c-prov"
    assert any(e["source_item_id"] == item for e in c0["evidence"])
    assert c0["evidence"][0]["source_tier"] == "TIER_1"
    assert prov["artifact"]["artifact_id"] == gen["artifact_id"]


# --------------------------------------------------------------------------- #
# 7. claim -> evidence preservation (every referenced claim keeps its evidence link)
# --------------------------------------------------------------------------- #
def test_claim_evidence_preservation():
    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-ev", "text": "The tax is three euros.", "source_id": "src-7"},
        ])
        item = str(s.query(claim_evidence).first().source_item_id)

    with get_session() as s:
        gen = generate_story(s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T)
        row = s.query(generated_artifacts).filter_by(artifact_id=gen["artifact_id"]).one()
        refs = from_jsonable(row.claim_refs_json)

    with get_session() as s:
        for key in refs:
            c = s.query(claims).filter_by(claim_id=key).one()
            evs = s.query(claim_evidence).filter_by(claim_id=str(c.id)).all()
            assert any(str(e.source_item_id) == item for e in evs), f"claim {key} lost its evidence link"


# --------------------------------------------------------------------------- #
# 8. unsupported claim is excluded and the artifact cannot publish
# --------------------------------------------------------------------------- #
def test_unsupported_claim_excluded_and_cannot_publish():
    with get_session() as s:
        result = _seed_story_with_claims(s, story_id="story-8", claim_specs=[
            {"claim_id": "c-supported", "text": "The tax is three euros.", "source_id": "src-8"},
            {"claim_id": "c-weak", "text": "Officials are considering a second increase next year.",
             "source_id": "src-8", "with_evidence": False},
        ])
    assert result["decision"] != "PUBLISH"

    with get_session() as s:
        gen = generate_story(s, story_id="story-8", format=ArtifactFormat.ARTICLE.value, reference_time=T)
        row = s.query(generated_artifacts).filter_by(artifact_id=gen["artifact_id"]).one()

    assert gen["publishable"] is False
    with get_session() as s:
        text = _body_text(s, gen["artifact_id"])
    assert "Officials are considering a second increase next year." not in text
    excluded = from_jsonable(row.excluded_claims_json)
    assert any(e["claim_id"] == "c-weak" and e["reason"] == "insufficient_evidence" for e in excluded)


# --------------------------------------------------------------------------- #
# 9. WAIT decision -> artifact cannot publish
# --------------------------------------------------------------------------- #
def test_wait_decision_cannot_publish():
    with get_session() as s:
        result = _seed_story_with_claims(s, story_id="story-9", claim_specs=[
            {"claim_id": "c-red", "text": "The minister is accused of embezzling public funds.",
             "source_id": "src-9"},
        ])
    assert result["decision"] == "WAIT"

    with get_session() as s:
        gen = generate_story(s, story_id="story-9", format=ArtifactFormat.ARTICLE.value, reference_time=T)
    assert gen["publishable"] is False
    # Structurally the artifact is still valid — the P3 decision gate blocks publication.
    assert gen["state"] == GenerationState.VALIDATED.value
    assert gen["validation"]["decision"]["decision"] == "WAIT"


# --------------------------------------------------------------------------- #
# 10. REVIEW decision -> artifact cannot publish
# --------------------------------------------------------------------------- #
def test_review_decision_cannot_publish():
    with get_session() as s:
        result = _seed_story_with_claims(s, story_id="story-10", claim_specs=[
            {"claim_id": "c-norev", "text": "The government approved the measure.",
             "source_id": "src-10", "with_evidence": False},
        ])
    assert result["decision"] == "REVIEW"

    with get_session() as s:
        gen = generate_story(s, story_id="story-10", format=ArtifactFormat.ARTICLE.value, reference_time=T)
    assert gen["publishable"] is False
    assert gen["validation"]["decision"]["decision"] == "REVIEW"


# --------------------------------------------------------------------------- #
# 11. REJECT decision -> artifact cannot publish
# --------------------------------------------------------------------------- #
def test_reject_decision_cannot_publish():
    with get_session() as s:
        result = _seed_story_with_claims(s, story_id="story-11", claim_specs=[
            {"claim_id": "c-rej", "text": "The minister is accused of embezzling public funds.",
             "source_id": "src-11", "with_evidence": False},
        ])
    assert result["decision"] == "REJECT"

    with get_session() as s:
        gen = generate_story(s, story_id="story-11", format=ArtifactFormat.ARTICLE.value, reference_time=T)
    assert gen["publishable"] is False
    assert gen["validation"]["decision"]["decision"] == "REJECT"


# --------------------------------------------------------------------------- #
# 12. generation never publishes directly (no P4/P5 rows are created by P6)
# --------------------------------------------------------------------------- #
def test_generation_never_publishes_directly():
    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-nopub", "text": "The tax is three euros.", "source_id": "src-12"},
        ])

    with get_session() as s:
        gen = generate_story(s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T)
    assert gen["publishable"] is True  # derived property only — nothing was actually published

    with get_session() as s:
        assert s.query(publications).count() == 0
        assert s.query(publication_attempts).count() == 0
        assert s.query(published_snapshots).count() == 0
        from newsforge.db import articles
        assert s.query(articles).count() == 0
        st = s.query(stories).filter_by(story_id="story-1").one()
        assert st.status == "ACTIVE"


# --------------------------------------------------------------------------- #
# 13. generation does not modify the persisted Decision
# --------------------------------------------------------------------------- #
def test_generation_does_not_modify_decision():
    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-dm", "text": "The tax is three euros.", "source_id": "src-13"},
        ])
        before = _decision_snapshot(s)

    with get_session() as s:
        generate_story(s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T)

    with get_session() as s:
        after = _decision_snapshot(s)
        assert s.query(decisions).count() == 1
    assert before == after


# --------------------------------------------------------------------------- #
# 14. generation does not modify trust evaluations
# --------------------------------------------------------------------------- #
def test_generation_does_not_modify_trust():
    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-tr", "text": "The tax is three euros.", "source_id": "src-14"},
        ])
        before = _eval_dumps(s)

    with get_session() as s:
        generate_story(s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T)

    with get_session() as s:
        after = _eval_dumps(s)
    assert before == after


# --------------------------------------------------------------------------- #
# 15. generation does not modify quality evaluations
# --------------------------------------------------------------------------- #
def test_generation_does_not_modify_quality():
    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-qa", "text": "The tax is three euros.", "source_id": "src-15"},
        ])
        before = _eval_dumps(s)

    with get_session() as s:
        generate_story(s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T)

    with get_session() as s:
        after = _eval_dumps(s)
    assert before == after


# --------------------------------------------------------------------------- #
# 16. determinism (pure assembly + persisted regeneration)
# --------------------------------------------------------------------------- #
def test_determinism():
    story = {"id": "pk-x", "story_id": "story-1", "title": "T", "summary": "S", "topic": None}
    claims_payload = [
        {"claim_id": "c1", "text": "A.", "has_evidence": True,
         "evidence_source_ids": ["i1"], "evidence_dates": ["2026-09-05T10:00:00+00:00"]},
        {"claim_id": "c2", "text": "B.", "has_evidence": False,
         "evidence_source_ids": [], "evidence_dates": []},
    ]
    a = assemble_editorial_artifact(story=story, claims=claims_payload, format="ARTICLE", reference_time=T)
    b = assemble_editorial_artifact(story=story, claims=claims_payload, format="ARTICLE", reference_time=T)
    assert a == b

    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-det", "text": "The tax is three euros.", "source_id": "src-16"},
        ])

    gens = []
    for _ in range(2):
        with get_session() as s:
            gens.append(generate_story(s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T))
    assert gens[0]["artifact_id"] == gens[1]["artifact_id"]
    assert gens[1]["created"] is False
    assert gens[0]["validation"]["checks"]["determinism_ok"] is True
    assert gens[0]["state"] == GenerationState.VALIDATED.value


# --------------------------------------------------------------------------- #
# 17. injectable reference_time (no hidden clock reads)
# --------------------------------------------------------------------------- #
def test_reference_time_injected():
    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-rt", "text": "The tax is three euros.", "source_id": "src-17"},
        ])

    with get_session() as s:
        gen = generate_story(s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T2)
        row = s.query(generated_artifacts).filter_by(artifact_id=gen["artifact_id"]).one()
    assert row.reference_time == T2
    assert from_jsonable(row.body_json)["reference_time"] == T2


# --------------------------------------------------------------------------- #
# 18. a failing generator leaves no partial writes
# --------------------------------------------------------------------------- #
def test_generator_failure_leaves_no_partial_writes():
    class FailingGenerator(DeterministicGenerator):
        def generate(self, **kw):
            raise RuntimeError("simulated generator failure")

    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-fail", "text": "The tax is three euros.", "source_id": "src-18"},
        ])
        decision_before = _decision_snapshot(s)
        evals_before = _eval_dumps(s)

    raised = False
    try:
        with get_session() as s:
            generate_story(s, story_id="story-1", generator=FailingGenerator())
    except RuntimeError:
        raised = True
    assert raised, "generator failure must propagate"

    with get_session() as s:
        assert s.query(generated_artifacts).count() == 0, "failure must not leave partial artifact rows"
        assert _decision_snapshot(s) == decision_before
        assert _eval_dumps(s) == evals_before


# --------------------------------------------------------------------------- #
# 19. an invalid artifact is explicitly non-publishable
# --------------------------------------------------------------------------- #
def test_invalid_artifact_is_explicitly_not_publishable():
    with get_session() as s:
        _seed_story_with_claims(s, claim_specs=[
            {"claim_id": "c-inv", "text": "The tax is three euros.", "source_id": "src-19"},
        ])

    with get_session() as s:
        gen = generate_story(s, story_id="story-1", format=ArtifactFormat.ARTICLE.value, reference_time=T)
    assert gen["state"] == GenerationState.VALIDATED.value and gen["publishable"] is True

    # Drift the inputs: the generator would now produce a different title for this story.
    with get_session() as s:
        st = s.query(stories).filter_by(story_id="story-1").one()
        st.title = "Changed after generation"
        s.commit()

    with get_session() as s:
        row = s.query(generated_artifacts).filter_by(artifact_id=gen["artifact_id"]).one()
        result = validate_generated_artifact(s, row, persist=True)

    assert result["valid"] is False
    assert "determinism_ok" in result["failures"]
    with get_session() as s:
        row2 = s.query(generated_artifacts).filter_by(artifact_id=gen["artifact_id"]).one()
        assert row2.state == GenerationState.INVALID.value
        assert row2.publishable is False


# --------------------------------------------------------------------------- #
# 20. full P2->P5 regression flow: verify -> generate -> publish -> measure -> snapshot
# --------------------------------------------------------------------------- #
def test_regression_p2_to_p5_full_flow():
    with get_session() as s:
        _seed_story_with_claims(s, story_id="story-e2e", claim_specs=[
            {"claim_id": "c-e2e", "text": "The tax is three euros.", "source_id": "src-e2e"},
        ])

    # P3 gate (real decision engine) must approve before anything moves.
    with get_session() as s:
        d = s.query(decisions).filter_by(target_type="STORY", target_id="story-e2e").one()
        assert d.decision == "PUBLISH"

    # P6 generation (never publishes by itself).
    with get_session() as s:
        gen = generate_story(s, story_id="story-e2e", format=ArtifactFormat.ARTICLE.value, reference_time=T)
        assert gen["state"] == GenerationState.VALIDATED.value and gen["publishable"] is True

    # P4 publication through the real publisher gate.
    with get_session() as s:
        pub = publish_story(s, story_id="story-e2e", destinations=["recording"])
        assert pub["blocked"] is False and pub["published"] is True

    # P5 measurement + snapshot of the REAL publication artifacts.
    with get_session() as s:
        m = record_publication_metrics(s, story_id="story-e2e", destination_key="recording", reference_time=T)
        assert m["found"] is True and m["success"] is True

        snap = capture_snapshot(s, story_id="story-e2e", reference_time=T)
        assert snap["found"] is True
        assert snap["snapshot"]["title"] == "Test Story"
