"""P1-P6 Pipeline Orchestrator.

Single, testable entry point that connects all pipeline phases:

    Source (P1) -> Story Detection (P2) -> Trust/Claims/Decision (P3) ->
    Decision Gate -> AI Generation (P4) -> Publish Gate -> Destination (P5) ->
    Measurement (P5/P6) -> Analytics (P6)

Design principles:
- Every step is explicit; gates are never skipped.
- REJECT/WAIT stop publication for a story.
- Idempotent: re-running with the same inputs never duplicates publications,
  snapshots, events, or AI cost rows.
- Deterministic when MOCK (no clock reads, no randomness in AI path).
- Errors propagate for critical failures; per-story failures are collected
  as structured StoryOutcomes with phase context.
- The orchestrator never re-derives editorial decisions; it consumes the
  persisted verdict produced by P3.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

import time

from newsforge.core.error_tracker import persist_error
from newsforge.core.logger import get_logger, log_event
from newsforge.core.metrics import metrics
from newsforge.db import (
    get_session, source_items, sources, stories, story_signals,
)
from newsforge.pipeline.context import new_run_context
from newsforge.verify.claims import evidence_matches

logger = get_logger("pipeline.orchestrator")


def _ms(started: float) -> int:
    return int(round((time.monotonic() - started) * 1000.0))


# --------------------------------------------------------------------------- #
# Public errors
# --------------------------------------------------------------------------- #
class PipelinePhaseError(RuntimeError):
    """A critical pipeline phase failed.

    Attributes carry the failing phase name, the story handle (if known), and
    the original exception so callers can log or retry with full context.
    """

    def __init__(
        self,
        *,
        phase: str,
        story_id: str | None = None,
        context: str = "",
        cause: Exception | None = None,
    ):
        self.phase = phase
        self.story_id = story_id
        self.context = context
        msg = f"Pipeline phase {phase!r} failed"
        if story_id:
            msg += f" for story {story_id!r}"
        if context:
            msg += f": {context}"
        super().__init__(msg)
        if cause is not None:
            self.__cause__ = cause


# --------------------------------------------------------------------------- #
# Structured result
# --------------------------------------------------------------------------- #
@dataclass
class StoryOutcome:
    """Per-story result of one pipeline run."""

    story_handle: str  # stories.id (UUID PK)
    business_key: str  # stories.story_id (deterministic cluster key)
    final_status: str  # PUBLISHED | BLOCKED | WAIT | REJECT | FAILED
    decision: dict | None = None
    artifact: dict | None = None
    publish: dict | None = None
    measurement: dict | None = None
    analytics: dict | None = None
    error: str | None = None


# --------------------------------------------------------------------------- #
# Default claim spec builder
# --------------------------------------------------------------------------- #
def _default_claim_spec_builder(
    session, story_handle: str, business_key: str, *, reference_time: str | None = None,
) -> list[dict]:
    """Build claim specs from a story's linked source items.

    Each persisted source_item linked via story_signals becomes one claim spec
    keyed by the STORY BUSINESS KEY (the deterministic cluster key). Tiers are
    resolved from the originating source. The claim_id is a deterministic SHA-256
    truncated to 16 hex chars so re-runs never duplicate claim rows (idempotent by
    UNIQUE constraint).

    Corroboration (G1, section 3): a claim's EVIDENCE is limited to the story items
    that genuinely support the SAME event as the claim's own item, decided by the
    deterministic :func:`evidence_matches` predicate. Empty/title-only text and
    coarse story buckets (topic+year) therefore never fabricate corroboration: two
    unrelated items inside one story stay single-source, while a BBC item and a
    Guardian item about the same event each expose the other as evidence. The claim's
    own item stays first so provenance (claim row ``source_item_id``/URL/date) keeps
    pointing at its true origin."""
    linked = session.query(story_signals).filter_by(story_id=business_key).all()
    items: list[Any] = []
    for sig in linked:
        item = session.get(source_items, str(sig.item_id))
        if item is not None:
            items.append(item)
    if not items:
        return []

    # Deterministic ordering (section 15): evidence sets, tiers and spec order must
    # not depend on DB insertion order when the pipeline is re-run.
    items.sort(key=lambda it: str(it.id))

    story_item_ids = [str(it.id) for it in items]
    tiers = sorted({_source_tier(session, it) for it in items})
    item_titles = {str(it.id): (it.title or "") for it in items}
    item_descriptions = {str(it.id): (it.description or "") for it in items}

    specs: list[dict] = []
    for item in items:
        text = (item.description or item.title or "").strip()
        if not text:
            continue
        own_id = str(item.id)
        # Own item first (keeps claim provenance on its true origin), then the story's
        # other items that support the same event, in deterministic id order.
        evidence = [own_id] + [
            i for i in story_item_ids
            if i != own_id
            and evidence_matches(
                subject_title=item_titles.get(own_id),
                subject_description=item_descriptions.get(own_id),
                candidate_title=item_titles.get(i),
                candidate_description=item_descriptions.get(i),
            )
        ]
        claim_id = hashlib.sha256(
            f"{story_handle}|{item.id}|{text}".encode()
        ).hexdigest()[:16]
        specs.append({
            "claim_id": claim_id,
            "text": text,
            "story_id": business_key,
            "source_item_ids": evidence,
            "tiers": tiers,
            "publication_date": getattr(item, "published_at", None),
        })
    return specs


def _source_tier(session, item) -> str:
    src = session.get(sources, str(item.source_id)) if item.source_id else None
    return str(src.tier).upper() if src and src.tier else "TIER_3"


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def run_pipeline(
    *,
    signal_ids: Sequence[str] | None = None,
    source: dict | None = None,
    claim_spec_builder: Callable | None = None,
    format: str = "ARTICLE",
    destinations: Sequence[str] | None = None,
    reference_time: str | datetime | None = None,
    ai_router: Any | None = None,
    register_defaults: bool = True,
    traffic_observations: Sequence[dict] | None = None,
    revenue_observations: Sequence[dict] | None = None,
) -> dict:
    """Run the full P1-P6 pipeline for all stories detected from the given signals.

    Parameters
    ----------
    signal_ids : list of str, optional
        UUIDs of already-persisted source_items (P1 input; fully deterministic).
    source : dict, optional
        Source record for async fetch via ``ingest_source`` (P1 network path;
        when given, ``signal_ids`` is ignored and items are fetched).
    claim_spec_builder : callable, optional
        ``(session, story_handle, business_key, reference_time=...) -> list[dict]``.
        Defaults to ``_default_claim_spec_builder`` which builds one claim per
        linked source_item.
    format : str
        Artifact format (default ``ARTICLE``).
    destinations : list of str, optional
        Destination keys to publish to (default: built-in recording).
    reference_time : str or datetime, optional
        Injectable clock for deterministic testing.
    ai_router : AiRouter, optional
        Override the default AiRouter (MOCK by default).
    register_defaults : bool
        Whether to reset and register built-in destinations (default True).
    traffic_observations : list of dict, optional
        Passed to ``record_traffic_event`` after successful publication.
    revenue_observations : list of dict, optional
        Passed to ``record_revenue_event`` after successful publication.

    Returns
    -------
    dict
        ``{status, stories_detected, stories_processed, outcomes: [StoryOutcome, ...]}``

    Raises
    ------
    PipelinePhaseError
        When a critical phase (detection, DB access) fails irrecoverably.
    """
    from newsforge.generate.assembly import generate_story
    from newsforge.measurement import record_destination_metrics
    from newsforge.publish import (
        publish_story, register_builtin_destinations, reset_registry,
    )
    from newsforge.stories.engine import StoryDetector
    from newsforge.verify.persist import run_verification

    # ------------------------------------------------------------------ #
    # Clock normalisation
    # ------------------------------------------------------------------ #
    if isinstance(reference_time, datetime):
        ref_iso = reference_time.isoformat()
    elif isinstance(reference_time, str):
        ref_iso = reference_time
    else:
        ref_iso = None  # let downstream modules derive from real timestamps

    # Operational correlation: one run_id per pipeline invocation (S10).
    ctx = new_run_context()
    log_event(logger, "pipeline_start", run_id=ctx.run_id, request_id=ctx.request_id)

    # ------------------------------------------------------------------ #
    # Destination registry
    # ------------------------------------------------------------------ #
    if register_defaults:
        reset_registry()
        register_builtin_destinations()

    # ------------------------------------------------------------------ #
    # Phase 1-2: Source -> Story Detection (critical)
    # ------------------------------------------------------------------ #
    if source is not None:
        import asyncio
        from newsforge.sources.engine import ingest_source

        t0 = time.monotonic()
        log_event(logger, "phase_start", phase="INGEST", run_id=ctx.run_id,
                  request_id=ctx.request_id)
        try:
            asyncio.run(ingest_source(source))
            log_event(logger, "phase_end", phase="INGEST", result="ok",
                      duration_ms=_ms(t0), run_id=ctx.run_id, request_id=ctx.request_id)
        except Exception as exc:
            log_event(logger, "phase_end", phase="INGEST", result="error",
                      error_type=type(exc).__name__, error_message=str(exc),
                      duration_ms=_ms(t0), run_id=ctx.run_id, request_id=ctx.request_id)
            metrics().inc("pipeline_runs_total", tags={"result": "failed"})
            metrics().inc("pipeline_failures_total",
                          tags={"phase": "INGEST", "error_type": type(exc).__name__})
            persist_error(module="pipeline.orchestrator",
                          error_type=type(exc).__name__, message=str(exc),
                          context={"run_id": ctx.run_id, "phase": "INGEST"})
            raise PipelinePhaseError(
                phase="INGEST", context=str(exc), cause=exc,
            ) from exc

    t0 = time.monotonic()
    log_event(logger, "phase_start", phase="DETECT", run_id=ctx.run_id,
              request_id=ctx.request_id)
    try:
        detector = StoryDetector()
        result = detector.process(signal_ids=signal_ids)
        log_event(logger, "phase_end", phase="DETECT", result="ok",
                  duration_ms=_ms(t0), run_id=ctx.run_id, request_id=ctx.request_id)
    except Exception as exc:
        log_event(logger, "phase_end", phase="DETECT", result="error",
                  error_type=type(exc).__name__, error_message=str(exc),
                  duration_ms=_ms(t0), run_id=ctx.run_id, request_id=ctx.request_id)
        metrics().inc("pipeline_runs_total", tags={"result": "failed"})
        metrics().inc("pipeline_failures_total",
                      tags={"phase": "DETECT", "error_type": type(exc).__name__})
        persist_error(module="pipeline.orchestrator",
                      error_type=type(exc).__name__, message=str(exc),
                      context={"run_id": ctx.run_id, "phase": "DETECT"})
        raise PipelinePhaseError(
            phase="DETECT", context=str(exc), cause=exc,
        ) from exc

    # ------------------------------------------------------------------ #
    # Per-story pipeline
    # ------------------------------------------------------------------ #
    outcomes: list[StoryOutcome] = []

    for view in result.stories:
        bk = view["story_id"]  # business key (deterministic cluster key)
        handle: str | None = None

        try:
            # Resolve UUID PK (the single pipeline handle for P3-P6)
            with get_session() as session:
                row = session.query(stories).filter_by(story_id=bk).first()
                if row is None:
                    outcomes.append(StoryOutcome(
                        story_handle="unknown",
                        business_key=bk,
                        final_status="FAILED",
                        error=f"story {bk!r} not found in DB after detection",
                    ))
                    continue
                handle = str(row.id)

            # Phase 3: Trust / Claims / Decision (keyed by the story BUSINESS key).
            with get_session() as session:
                builder = claim_spec_builder or _default_claim_spec_builder
                specs = builder(session, handle, bk, reference_time=ref_iso)
                if not specs:
                    outcomes.append(StoryOutcome(
                        story_handle=handle,
                        business_key=bk,
                        final_status="BLOCKED",
                        error="no claim specs (no linked items with text content)",
                    ))
                    continue

            t0 = time.monotonic()
            log_event(logger, "phase_start", phase="VERIFY", story_id=handle or bk,
                      run_id=ctx.run_id, request_id=ctx.request_id)
            verification = run_verification(
                claims_specs=specs,
                story_id=bk,
                reference_time=ref_iso,
            )
            log_event(logger, "phase_end", phase="VERIFY", result="ok",
                      duration_ms=_ms(t0), run_id=ctx.run_id,
                      request_id=ctx.request_id)

            # Phase 4: Decision Gate
            decision = verification["decision"]
            if decision != "PUBLISH":
                log_event(logger, "story_outcome", story_id=handle or bk,
                          result=("REJECT" if decision == "REJECT" else "WAIT"),
                          run_id=ctx.run_id, request_id=ctx.request_id)
                outcomes.append(StoryOutcome(
                    story_handle=handle,
                    business_key=bk,
                    final_status="REJECT" if decision == "REJECT" else "WAIT",
                    decision=verification,
                ))
                continue

            # Phase 5-8: Generate -> Publish -> Measure
            with get_session() as session:
                t0 = time.monotonic()
                log_event(logger, "phase_start", phase="GENERATE", story_id=handle or bk,
                          run_id=ctx.run_id, request_id=ctx.request_id)
                artifact = generate_story(
                    session,
                    story_id=bk,
                    format=format,
                    reference_time=ref_iso,
                    ai_router=ai_router,
                )
                log_event(logger, "phase_end", phase="GENERATE", result="ok",
                          artifact_id=(artifact or {}).get("artifact_id"),
                          duration_ms=_ms(t0), run_id=ctx.run_id,
                          request_id=ctx.request_id)

                t0 = time.monotonic()
                log_event(logger, "phase_start", phase="PUBLISH", story_id=handle or bk,
                          run_id=ctx.run_id, request_id=ctx.request_id)
                pub = publish_story(
                    session,
                    story_id=bk,
                    destinations=destinations,
                )
                log_event(logger, "phase_end", phase="PUBLISH",
                          result="published" if pub.get("published") else "not_published",
                          duration_ms=_ms(t0), run_id=ctx.run_id,
                          request_id=ctx.request_id)

                measurement = None
                if pub.get("published"):
                    measurement = record_destination_metrics(
                        session,
                        story_id=bk,
                        reference_time=ref_iso,
                    )

            # Phase 9: Analytics (only when caller provides observations)
            analytics_result = None
            if traffic_observations or revenue_observations:
                from newsforge.analytics.events import (
                    record_revenue_event, record_traffic_event,
                )

                with get_session() as session:
                    for obs in (traffic_observations or []):
                        record_traffic_event(session, entity_id=bk, **obs)
                    for obs in (revenue_observations or []):
                        record_revenue_event(session, entity_id=bk, **obs)
                    session.commit()
                analytics_result = {
                    "traffic": len(traffic_observations or []),
                    "revenue": len(revenue_observations or []),
                }

            outcomes.append(StoryOutcome(
                story_handle=handle,
                business_key=bk,
                final_status="PUBLISHED" if pub.get("published") else "BLOCKED",
                decision=verification,
                artifact=artifact,
                publish=pub,
                measurement=measurement,
                analytics=analytics_result,
            ))

        except PipelinePhaseError:
            metrics().inc("pipeline_runs_total", tags={"result": "failed"})
            raise
        except Exception as exc:
            log_event(logger, "story_failed", story_id=bk, story_handle=handle or "unknown",
                      error_type=type(exc).__name__, error_message=str(exc),
                      run_id=ctx.run_id, request_id=ctx.request_id)
            metrics().inc("pipeline_failures_total",
                          tags={"phase": "STORY", "error_type": type(exc).__name__})
            persist_error(module="pipeline.orchestrator",
                          error_type=type(exc).__name__, message=str(exc),
                          context={"run_id": ctx.run_id, "request_id": ctx.request_id,
                                   "story_id": bk, "story_handle": handle or "unknown"})
            outcomes.append(StoryOutcome(
                story_handle=handle or "unknown",
                business_key=bk,
                final_status="FAILED",
                error=f"{type(exc).__name__}: {exc}",
            ))

    metrics().inc("pipeline_runs_total", tags={"result": "ok"})
    log_event(logger, "pipeline_end", result="ok",
              stories_detected=len(result.stories), stories_processed=len(outcomes),
              duration_ms=_ms(ctx.started_at), run_id=ctx.run_id,
              request_id=ctx.request_id)

    return {
        "status": "ok",
        "stories_detected": len(result.stories),
        "stories_processed": len(outcomes),
        "outcomes": outcomes,
    }
