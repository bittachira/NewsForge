"""P6 — artifact assembly, generation orchestration, validation and provenance.

``assemble_editorial_artifact`` is a pure function (no I/O): it turns a story dict plus the
supplied claim dicts into a :class:`GeneratedContent` for one format. ``generate_story`` is
the DB-facing orchestrator: it READS stories/claims/evidence/decisions, runs the generator,
validates structurally and persists ONE artifact row per logical generation (idempotent).

Safety contract (§11): this layer never writes to ``decisions``, ``trust_evaluations``,
``quality_evaluations``, ``articles`` or ``stories`` and never publishes — the single commit
happens only after generation AND validation succeed, so a failing generator leaves no
partial write. ``publishable`` is a derived property only; the authoritative gate stays
:func:`newsforge.verify.persist.is_auto_publishable`."""
from __future__ import annotations

import hashlib
from typing import Optional

from newsforge.db.base import ts
from newsforge.db.models import (
    ArtifactFormat,
    GenerationState,
    claim_evidence,
    claims,
    decisions,
    generated_artifacts,
    source_items,
    sources,
    stories,
    from_jsonable,
    to_jsonable,
)
from newsforge.verify.persist import is_auto_publishable

from .generator import (
    GENERATOR_VERSION,
    MODEL_NAME,
    TEMPLATE_VERSION,
    DeterministicGenerator,
)


_FORMAT_VALUES = frozenset(f.value for f in ArtifactFormat)


# --------------------------------------------------------------------------- #
# Deterministic artifact identity
# --------------------------------------------------------------------------- #
def derive_artifact_id(story_id: str, format: str, generator_version: str, template_version: str) -> str:
    """Deterministic business key for a logical generation (SHA-256).

    Same (story, format, versions) always maps to the same row — this is what makes
    persistence idempotent."""
    payload = f"{story_id}|{format}|{generator_version}|{template_version}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Pure template assembly (no I/O, no clock reads, no randomness)
# --------------------------------------------------------------------------- #
def _base_title(story: dict) -> str:
    return (story.get("title") or story.get("topic") or f"Story {story.get('story_id')}").strip()


def _first_sentence(text: str) -> str:
    t = " ".join((text or "").split())
    if not t:
        return ""
    for sep in (". ", "! ", "? "):
        idx = t.find(sep)
        if idx != -1:
            return t[:idx]
    return t


def _sections_for(fmt: str, story: dict, used: list) -> list:
    title = _base_title(story)
    intro = (story.get("summary") or "").strip()

    if fmt == ArtifactFormat.ARTICLE.value:
        sections = [{"type": "intro", "text": intro or f"{title}."}]
        for c in used:
            sections.append({"type": "fact", "claim_id": c["claim_id"], "text": c["text"]})
        return sections

    if fmt == ArtifactFormat.BRIEF.value:
        if used:
            return [{"type": "bullet", "claim_id": c["claim_id"], "text": c["text"]} for c in used]
        return [{"type": "note", "text": "Sin claims con evidencia suficiente para el brief."}]

    if fmt == ArtifactFormat.NEWSLETTER.value:
        sections = [{"type": "greeting", "text": f"Resumen de: {title}"}]
        for c in used:
            sections.append({"type": "bullet", "claim_id": c["claim_id"], "text": c["text"]})
        sections.append({"type": "footer", "text": "Generado localmente a partir de claims verificados."})
        return sections

    if fmt == ArtifactFormat.SOCIAL.value:
        if used:
            text = " ".join(c["text"] for c in used[:2])
            return [{"type": "post", "text": f"{title}: {text}"}]
        return [{"type": "note", "text": f"{title} (sin claims verificados)"}]

    if fmt == ArtifactFormat.VIDEO_SCRIPT.value:
        sections = [{"type": "vo_intro", "text": f"{title}. {intro}".strip()}]
        for c in used:
            sections.append({"type": "vo_fact", "claim_id": c["claim_id"], "text": c["text"]})
        sections.append({"type": "vo_outro", "text": "Fin del guion."})
        return sections

    if fmt == ArtifactFormat.TIMELINE.value:
        ordered = sorted(used, key=lambda c: ((c.get("evidence_dates") or [""])[0], c["claim_id"]))
        if ordered:
            return [
                {"type": "event", "claim_id": c["claim_id"],
                 "date": (c.get("evidence_dates") or [""])[0], "text": c["text"]}
                for c in ordered
            ]
        return [{"type": "note", "text": "Sin eventos con evidencia suficiente."}]

    if fmt == ArtifactFormat.FAQ.value:
        if used:
            return [
                {"type": "qa", "claim_id": c["claim_id"],
                 "question": _first_sentence(c["text"]) + "?", "answer": c["text"]}
                for c in used
            ]
        return [{"type": "note", "text": "Sin preguntas con evidencia suficiente."}]

    raise ValueError(f"unknown artifact format: {fmt!r}")


def assemble_editorial_artifact(
    *,
    story: dict,
    claims: list,
    format: str = ArtifactFormat.ARTICLE.value,
    reference_time: Optional[str] = None,
    generator_version: str = GENERATOR_VERSION,
    template_version: str = TEMPLATE_VERSION,
    model_name: str = MODEL_NAME,
) -> "GeneratedContent":
    """Pure assembly: (story dict, claim dicts, format, clock) -> GeneratedContent.

    No I/O, no clock reads, no randomness: identical inputs always produce identical output
    (§15). Claims without evidence are excluded from the facts and recorded in
    ``excluded_claims`` with reason ``"insufficient_evidence"`` — never stated as facts and
    never fabricated."""
    fmt = str(format)
    if fmt not in _FORMAT_VALUES:
        raise ValueError(f"unknown artifact format: {fmt!r}")

    used = [c for c in claims if c.get("has_evidence")]
    excluded = [
        {"claim_id": c["claim_id"], "text": c["text"], "reason": "insufficient_evidence"}
        for c in claims if not c.get("has_evidence")
    ]

    title = _base_title(story)
    summary = (story.get("summary") or "").strip() or (used[0]["text"] if used else None)
    body_json = {"format": fmt, "reference_time": reference_time, "sections": _sections_for(fmt, story, used)}

    from .generator import GeneratedContent  # lazy: generator imports this module at load time

    return GeneratedContent(
        title=title,
        summary=summary,
        body_json=body_json,
        claim_refs=[c["claim_id"] for c in used],
        excluded_claims=excluded,
        deterministic=True,
        generator_version=generator_version,
        template_version=template_version,
        model_name=model_name,
    )


# --------------------------------------------------------------------------- #
# DB-facing helpers (read-only over editorial tables)
# --------------------------------------------------------------------------- #
def _story_dict(row) -> dict:
    return {
        "id": str(row.id),
        "story_id": row.story_id,
        "title": row.title,
        "summary": row.summary,
        "topic": row.topic,
    }


def _load_claims_payload(session, story_id: str) -> list:
    """Load the story's claims with their evidence links (READ ONLY).

    ``claim_evidence.claim_id`` references ``claims.id`` (PK), not the business key — so the
    join target is ``str(row.id)``."""
    rows = session.query(claims).filter_by(story_id=str(story_id)).order_by(claims.claim_id).all()
    payload = []
    for row in rows:
        ev_rows = session.query(claim_evidence).filter_by(claim_id=str(row.id)).all()
        item_ids = sorted({str(e.source_item_id) for e in ev_rows})
        dates = []
        for iid in item_ids:
            item = session.get(source_items, iid)
            if item is not None and item.published_at:
                dates.append(str(item.published_at))
        payload.append({
            "claim_id": row.claim_id,
            "text": row.text,
            "has_evidence": len(ev_rows) > 0,
            "evidence_source_ids": item_ids,
            "evidence_dates": sorted(dates),
        })
    return payload


def _has_evidence(session, claim_row) -> bool:
    return session.query(claim_evidence).filter_by(claim_id=str(claim_row.id)).count() > 0


# --------------------------------------------------------------------------- #
# Validation (structured + auditable)
# --------------------------------------------------------------------------- #
def validate_generated_artifact(session, artifact, *, persist: bool = False) -> dict:
    """Structurally validate a generated artifact against the REAL tables.

    Checks: story/format/title/body/versions present; referenced claims exist and have
    evidence; no claim lacking evidence appears as a fact; provenance reconstructable;
    determinism (when the generator is deterministic — regenerated with the STORED
    reference_time); decision compatibility. With ``persist=True`` only ``state``,
    ``publishable`` and ``validation_json`` are updated on the row.

    Publication rule: ``publishable = valid AND decision_row exists AND is_auto_publishable`` —
    the publisher's gate stays authoritative; P6 never re-derives or overrides it."""
    fmt = str(artifact.format)
    checks: dict = {}

    story_row = session.query(stories).filter_by(story_id=str(artifact.story_id)).first()
    checks["story_exists"] = story_row is not None
    checks["format_valid"] = fmt in _FORMAT_VALUES
    checks["title_present"] = bool(artifact.title and str(artifact.title).strip())

    body = from_jsonable(artifact.body_json)
    sections = (body or {}).get("sections") if isinstance(body, dict) else None
    checks["body_present"] = isinstance(sections, list) and len(sections) > 0

    checks["generator_version_present"] = bool(artifact.generator_version)
    checks["template_version_present"] = bool(artifact.template_version)

    claim_rows: dict = {}
    if story_row is not None:
        claim_rows = {r.claim_id: r for r in
                      session.query(claims).filter_by(story_id=str(artifact.story_id)).all()}

    refs = from_jsonable(artifact.claim_refs_json) or []
    checks["claim_refs_exist"] = isinstance(refs, list) and all(str(r) in claim_rows for r in refs)

    ref_rows = [claim_rows[str(r)] for r in refs if str(r) in claim_rows]
    checks["claim_refs_have_evidence"] = all(_has_evidence(session, r) for r in ref_rows)

    body_text = " ".join(str(s.get("text", "")) for s in (sections or [])) + " " + str(artifact.summary or "")
    unsupported = [r for r in claim_rows.values() if not _has_evidence(session, r)]
    checks["no_unsupported_facts"] = all(not (r.text and r.text in body_text) for r in unsupported)

    prov = reconstruct_generation_provenance(session, artifact=artifact)
    checks["provenance_reconstructable"] = bool(prov.get("chain_complete"))

    if artifact.deterministic:
        regen = assemble_editorial_artifact(
            story=_story_dict(story_row),
            claims=_load_claims_payload(session, str(artifact.story_id)),
            format=fmt,
            reference_time=artifact.reference_time,
            generator_version=str(artifact.generator_version),
            template_version=str(artifact.template_version),
        )
        checks["determinism_ok"] = (
            regen.title == artifact.title
            and regen.summary == artifact.summary
            and regen.body_json == body
            and sorted(regen.claim_refs) == sorted(str(r) for r in refs)
        )
    else:
        checks["determinism_ok"] = True

    decision_row = (session.query(decisions)
                    .filter_by(target_type="STORY", target_id=str(artifact.story_id)).first())
    if decision_row is None:
        checks["decision_compatible"] = not bool(artifact.publishable)
    else:
        # The artifact may never claim more publishability than the persisted P3 verdict allows.
        checks["decision_compatible"] = (not bool(artifact.publishable)) or is_auto_publishable(decision_row)

    failures = [name for name, ok in checks.items() if not ok]
    result = {
        "valid": not failures,
        "checks": checks,
        "failures": failures,
        "decision": (
            {"id": str(decision_row.id), "decision": decision_row.decision,
             "human_override": bool(decision_row.human_override)}
            if decision_row is not None else None
        ),
    }

    if persist:
        artifact.state = GenerationState.VALIDATED.value if result["valid"] else GenerationState.INVALID.value
        artifact.publishable = bool(result["valid"] and decision_row is not None and is_auto_publishable(decision_row))
        artifact.validation_json = to_jsonable(result)
        session.commit()

    return result


# --------------------------------------------------------------------------- #
# Provenance: Story -> Claim -> Evidence/Source Item -> Decision -> Artifact
# --------------------------------------------------------------------------- #
def reconstruct_generation_provenance(session, *, artifact=None, artifact_id: Optional[str] = None,
                                      story_id: Optional[str] = None) -> dict:
    """Reconstruct the full generation audit chain from the REAL tables.

    Story -> Claim -> Evidence/Source Item -> Decision -> Generated Artifact. Pure reads;
    returns a nested structure plus ``chain_complete`` (every link of the chain resolves)."""
    row = artifact
    if row is None and artifact_id is not None:
        row = session.query(generated_artifacts).filter_by(artifact_id=str(artifact_id)).first()
    if row is None and story_id is not None:
        row = (session.query(generated_artifacts)
               .filter_by(story_id=str(story_id))
               .order_by(generated_artifacts.created_at.desc()).first())
    if row is None:
        return {"chain_complete": False, "reason": "no generated artifact found",
                "story": None, "claims": [], "excluded_claims": [], "decision": None, "artifact": None}

    story_row = session.query(stories).filter_by(story_id=str(row.story_id)).first()
    decision_row = session.get(decisions, str(row.decision_id)) if row.decision_id else None

    refs = from_jsonable(row.claim_refs_json) or []
    excluded = from_jsonable(row.excluded_claims_json) or []
    claim_rows: dict = {}
    if story_row is not None:
        claim_rows = {r.claim_id: r for r in
                      session.query(claims).filter_by(story_id=str(row.story_id)).all()}

    claims_out = []
    all_linked = True
    for key in refs:
        c = claim_rows.get(str(key))
        if c is None:
            all_linked = False
            continue
        ev_rows = session.query(claim_evidence).filter_by(claim_id=str(c.id)).all()
        evidence = []
        linked = False
        for ev in ev_rows:
            item = session.get(source_items, str(ev.source_item_id)) if ev.source_item_id else None
            source = (session.get(sources, str(item.source_id))
                      if (item is not None and item.source_id) else None)
            if item is not None:
                linked = True
            evidence.append({
                "claim_evidence_id": str(ev.id),
                "source_item_id": str(ev.source_item_id),
                "item_title": item.title if item is not None else None,
                "source_name": source.name if source is not None else None,
                "source_tier": source.tier if source is not None else None,
            })
        if not linked:
            all_linked = False
        claims_out.append({
            "claim_id_key": c.claim_id,
            "id": str(c.id),
            "text": c.text,
            "status": c.status,
            "evidence": evidence,
        })

    chain_complete = (
        story_row is not None
        and decision_row is not None
        and all_linked
        and len(claims_out) == len(refs)
    )

    return {
        "chain_complete": bool(chain_complete),
        "story": ({"id": str(story_row.id), "story_id": story_row.story_id,
                   "title": story_row.title, "slug": story_row.slug}
                  if story_row is not None else None),
        "claims": claims_out,
        "excluded_claims": excluded,
        "decision": (
            {"id": str(decision_row.id), "decision": decision_row.decision,
             "risk_level": decision_row.risk_level, "human_override": bool(decision_row.human_override),
             "policy_version": decision_row.policy_version}
            if decision_row is not None else None
        ),
        "artifact": {
            "artifact_id": row.artifact_id,
            "story_id": row.story_id,
            "format": row.format,
            "state": row.state,
            "publishable": bool(row.publishable),
            "generator_version": row.generator_version,
            "template_version": row.template_version,
            "reference_time": row.reference_time,
        },
    }


# --------------------------------------------------------------------------- #
# Orchestration: generate + validate + persist (single commit)
# --------------------------------------------------------------------------- #
def generate_story(session, *, story_id: str, format: str = ArtifactFormat.ARTICLE.value,
                   generator=None, reference_time: Optional[str] = None) -> dict:
    """Generate + persist ONE artifact for (story, format).

    1. READS story/claims/evidence/decision (never mutates them).
    2. Runs the generator (may raise -> nothing has been written yet, so no partial state).
    3. Validates structurally against the real tables.
    4. Persists idempotently by ``artifact_id`` with a SINGLE commit at the end.

    ``publishable`` is derived only: valid AND persisted decision exists AND
    :func:`is_auto_publishable` — it never replaces the publisher's gate (§11)."""
    fmt = str(format)
    if fmt not in _FORMAT_VALUES:
        raise ValueError(f"unknown artifact format: {fmt!r}")
    gen = generator or DeterministicGenerator()
    effective_time = reference_time if reference_time is not None else ts()

    story_row = session.query(stories).filter_by(story_id=str(story_id)).first()
    if story_row is None:
        raise ValueError(f"story {story_id!r} does not exist")

    decision_row = (session.query(decisions)
                    .filter_by(target_type="STORY", target_id=str(story_id)).first())

    # 1. Generate (may raise -> no writes have happened yet, so no partial state).
    content = gen.generate(
        story=_story_dict(story_row),
        claims=_load_claims_payload(session, str(story_id)),
        format=fmt,
        reference_time=effective_time,
    )

    artifact_id = derive_artifact_id(str(story_id), fmt, content.generator_version, content.template_version)

    # 2. Structural validation (reads only; the row is transient at this point).
    provisional = generated_artifacts(
        artifact_id=artifact_id,
        story_id=str(story_id),
        decision_id=(str(decision_row.id) if decision_row is not None else None),
        format=fmt,
        title=content.title,
        summary=content.summary,
        body_json=to_jsonable(content.body_json),
        claim_refs_json=to_jsonable(content.claim_refs),
        excluded_claims_json=to_jsonable(content.excluded_claims),
        generator_version=content.generator_version,
        template_version=content.template_version,
        model_name=content.model_name,
        deterministic=bool(content.deterministic),
        state=GenerationState.GENERATED.value,
        publishable=False,
        reference_time=effective_time,
    )
    validation = validate_generated_artifact(session, provisional)

    # 3. Derived publishability: never stronger than the P4 gate on the persisted decision.
    publishable = bool(validation["valid"] and decision_row is not None and is_auto_publishable(decision_row))
    validation["publishable"] = publishable
    state = GenerationState.VALIDATED.value if validation["valid"] else GenerationState.INVALID.value

    # 4. Idempotent persistence: single commit only after everything succeeded.
    existing = session.query(generated_artifacts).filter_by(artifact_id=artifact_id).first()
    created = existing is None
    row = existing or provisional
    if created:
        session.add(row)
    row.story_id = str(story_id)
    row.decision_id = provisional.decision_id
    row.format = fmt
    row.title = content.title
    row.summary = content.summary
    row.body_json = to_jsonable(content.body_json)
    row.claim_refs_json = to_jsonable(content.claim_refs)
    row.excluded_claims_json = to_jsonable(content.excluded_claims)
    row.generator_version = content.generator_version
    row.template_version = content.template_version
    row.model_name = content.model_name
    row.deterministic = bool(content.deterministic)
    row.state = state
    row.publishable = publishable
    row.validation_json = to_jsonable(validation)
    row.reference_time = effective_time
    session.commit()

    return {
        "artifact_id": artifact_id,
        "story_id": str(story_id),
        "format": fmt,
        "state": state,
        "publishable": publishable,
        "created": created,
        "validation": validation,
    }
