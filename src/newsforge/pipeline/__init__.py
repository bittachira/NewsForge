"""NewsForge Pipeline Orchestrator (P1-P6).

Single entry point connecting all editorial pipeline phases.

Call ``run_pipeline()`` to execute the full flow::

    from newsforge.pipeline import run_pipeline

    result = run_pipeline(signal_ids=["uuid-1", "uuid-2"], reference_time="2026-09-05T12:00:00+00:00")

Phases (executed per detected story):
    1. DETECT   -- StoryDetector clusters signals into stories (P2)
    2. VERIFY   -- run_verification builds claims, evaluates trust/quality, persists decision (P3)
    3. GATE     -- decision must be PUBLISH; WAIT/REJECT stop the story
    4. GENERATE -- generate_story produces a validated artifact with AI cost (P4, MOCK default)
    5. GATE2    -- publish only if is_auto_publishable on persisted decision
    6. PUBLISH  -- publish_story distributes via Destination abstraction (P5)
    7. MEASURE  -- record_destination_metrics captures publication/destination metrics (P5/P6)
    8. ANALYTICS-- record traffic/revenue events ONLY if caller provides observations (P6, never invented)

Final states per story:
    PUBLISHED  -- artifact generated and published to all destinations
    BLOCKED    -- no claim specs available (items lack text content)
    WAIT       -- decision engine returned WAIT (unverified, requires human review)
    REJECT     -- RED risk + unsupported claim; auto-rejected
    FAILED     -- exception during a phase; error message included

Idempotency guarantee:
    Re-running with the same signal_ids and reference_time never creates duplicate:
    - story_signals links (UNIQUE constraint)
    - claim rows (UNIQUE on claim_id)
    - claim_evidence links (UNIQUE on claim_id, source_item_id)
    - generated_artifacts (deterministic artifact_id, single commit)
    - ai_jobs/ai_runs (idempotent by run_id == artifact_id)
    - publications (UNIQUE on story_id + destination_key, skipped when completed)
    - publication_metrics / destination_metrics (UNIQUE on story_id + reference_time)
    - published_snapshots (UNIQUE on story_id + reference_time)

Destination abstraction:
    ``Destination`` is an ABC with a single ``publish(payload) -> DistributionOutcome`` method.
    ``RecordingDestination`` is the built-in stub (no network, records to DB).
    Production destinations implement the ABC and register via ``register(key, cls)``.

Pipeline handle:
    All internal phases use ``stories.id`` (UUID PK) as the single canonical handle.
    This is backward-compatible: callers passing the business key (``stories.story_id``)
    are resolved via ``or_(id==, story_id==)`` in generate_story and build_provenance_chain.
"""
from .orchestrator import PipelinePhaseError, StoryOutcome, run_pipeline

__all__ = [
    "run_pipeline",
    "PipelinePhaseError",
    "StoryOutcome",
]
