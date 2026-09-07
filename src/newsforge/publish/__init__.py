"""NewsForge publisher layer (section 24).

Public API only. The publisher is the ONLY component allowed to move content out of NewsForge: it
consumes a *persisted* Decision Engine verdict and distributes it across registered destinations.
No trust/quality/risk re-evaluation happens here -- that stays in :mod:`newsforge.verify` (§11)."""
from __future__ import annotations

from .destinations import (
    Destination,
    DistributionOutcome,
    RecordingDestination,
    available_keys,
    get_destination,
    register,
    register_builtin_destinations,
    reset_registry,
)
from .state import InvalidTransitionError, PublicationStateMachine, can_transition
from .publisher import (
    idempotency_key,
    publish_story,
    reconstruct_chain,
    retry_publication,
)

__all__ = [
    # publisher
    "publish_story",
    "retry_publication",
    "reconstruct_chain",
    "idempotency_key",
    # state machine
    "PublicationStateMachine",
    "can_transition",
    "InvalidTransitionError",
    # destinations + registry
    "Destination",
    "DistributionOutcome",
    "RecordingDestination",
    "register",
    "register_builtin_destinations",
    "available_keys",
    "get_destination",
    "reset_registry",
]
