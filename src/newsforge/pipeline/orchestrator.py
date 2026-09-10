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
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

from newsforge.db import (
    get_session, source_items, sources, stories, story_signals,
)

logger = logging.getLogger("newsforge.pipeline")


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
    keyed by the UUID PK story_handle.  Tiers are resolved from the originating
    source.  The claim_id is a deterministic SHA-256 truncated to 16 hex chars
    so re-runs never duplicate claim rows (idempotent by UNIQUE constraint)."""
    linked = session.query(story_signals).filter_by(story_id=business_key).all()
    specs: list[dict] = []
    for sig in linked:
        item = session.get(source_items, str(sig.item_id))
        if item is None:
            continue
        text = (item.description or item.title or "").strip()
        if not text:
            continue
        src = session.get(sources, str(item.source_id)) if item.source_id else None
        tier = str(src.tier).upper() if src and src.tier else "TIER_3"
        claim_id = hashlib.sha256(
            f"{story_handle}|{item.id}|{text}".encode()
        ).hexdigest()[:16]
        specs.append({
            "claim_id": claim_id,
            "text": text,
            "story_id": story_handle,
            "source_item_ids": [str(item.id)],
            "tiers": [tier],
            "publication_date": getattr(item, "published_at", None),
        })
    return specs


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

        try:
            asyncio.run(ingest_source(source))
        except Exception as exc:
            raise PipelinePhaseError(
                phase="INGEST", context=str(exc), cause=exc,
            ) from exc

    try:
        detector = StoryDetector()
        result = detector.process(signal_ids=signal_ids)
    except Exception as exc:
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

            # Phase 3: Trust / Claims / Decision
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

            verification = run_verification(
                claims_specs=specs,
                story_id=handle,
                reference_time=ref_iso,
            )

            # Phase 4: Decision Gate
            decision = verification["decision"]
            if decision != "PUBLISH":
                outcomes.append(StoryOutcome(
                    story_handle=handle,
                    business_key=bk,
                    final_status="REJECT" if decision == "REJECT" else "WAIT",
                    decision=verification,
                ))
                continue

            # Phase 5-8: Generate -> Publish -> Measure
            with get_session() as session:
                artifact = generate_story(
                    session,
                    story_id=handle,
                    format=format,
                    reference_time=ref_iso,
                    ai_router=ai_router,
                )

                pub = publish_story(
                    session,
                    story_id=handle,
                    destinations=destinations,
                )

                measurement = None
                if pub.get("published"):
                    measurement = record_destination_metrics(
                        session,
                        story_id=handle,
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
                        record_traffic_event(session, entity_id=handle, **obs)
                    for obs in (revenue_observations or []):
                        record_revenue_event(session, entity_id=handle, **obs)
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
            raise
        except Exception as exc:
            logger.error(
                "Pipeline failed at story %r: %s", bk, exc, exc_info=True,
            )
            outcomes.append(StoryOutcome(
                story_handle=handle or "unknown",
                business_key=bk,
                final_status="FAILED",
                error=f"{type(exc).__name__}: {exc}",
            ))

    return {
        "status": "ok",
        "stories_detected": len(result.stories),
        "stories_processed": len(outcomes),
        "outcomes": outcomes,
    }
