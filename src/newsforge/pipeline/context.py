"""Pipeline run correlation context (OPS_HARDENING_OBSERVABILITY S10).

One ``PipelineRunContext`` is created per ``run_pipeline`` invocation and threaded
through the phase instrumentation so ``phase_start``/``phase_end`` (and any persisted
error) carry a shared ``run_id`` and the originating ``request_id`` (when the pipeline
was triggered from an HTTP request). ``started_at`` uses ``time.monotonic`` so phase
durations are unaffected by wall-clock changes.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PipelineRunContext:
    run_id: str
    request_id: str | None = None
    started_at: float = field(default_factory=time.monotonic)


def new_run_context(request_id: str | None = None) -> PipelineRunContext:
    """Create a fresh run context; borrows the active HTTP request id when present."""
    from newsforge.core.request_context import get_request_id

    return PipelineRunContext(
        run_id=uuid.uuid4().hex,
        request_id=request_id if request_id is not None else get_request_id(),
    )