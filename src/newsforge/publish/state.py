"""Publication state machine (section 24).

The publisher owns this small, explicit lifecycle. It is deliberately INDEPENDENT of the P1/P2
ArticleStatus / StoryStatus enums: a distribution failure on one channel must never corrupt the
story's own lifecycle (§24). Transitions are validated against a fixed table so invalid moves are
rejected deterministically rather than silently accepted.

The machine is pure: :func:`can_transition` answers validity questions without side effects, and
:class:`PublicationStateMachine` applies an allowed transition in place. This keeps the rule set
testable and makes the state changes auditable (Story -> Decision -> Publication -> Attempt).
"""
from __future__ import annotations

from newsforge.db.models import PublicationStatus


class InvalidTransitionError(ValueError):
    """Raised when a requested publication-state change is not an allowed transition."""


# Explicit, ordered set of valid transitions: (from_state, to_state).
# Every state except COMPLETED has at least one outgoing edge; COMPLETED is terminal.
VALID_TRANSITIONS: frozenset[tuple[PublicationStatus, PublicationStatus]] = frozenset(
    {
        # A brand-new publication begins distributing, unless blocked or immediately failed.
        (PublicationStatus.PENDING, PublicationStatus.PUBLISHING),
        (PublicationStatus.PENDING, PublicationStatus.SUPPRESSED),  # decision engine blocked it
        (PublicationStatus.PENDING, PublicationStatus.FAILED),      # nothing to distribute / all failed
        (PublicationStatus.PENDING, PublicationStatus.COMPLETED),   # created + every channel succeeded (atomic)
        # While distributing: either everything succeeds or one channel fails.
        (PublicationStatus.PUBLISHING, PublicationStatus.COMPLETED),
        (PublicationStatus.PUBLISHING, PublicationStatus.FAILED),
        # Terminal state for retirement; can be reactivated for a later retry.
        (PublicationStatus.ARCHIVED, PublicationStatus.PENDING),
        # Retry edges: any non-terminal state may go back to PENDING so a fixed channel is retried.
        (PublicationStatus.SUPPRESSED, PublicationStatus.PENDING),
        (PublicationStatus.FAILED, PublicationStatus.PENDING),       # retry start
        (PublicationStatus.FAILED, PublicationStatus.COMPLETED),     # retry succeeded
    },
)


def can_transition(from_state: str | PublicationStatus, to_state: str | PublicationStatus) -> bool:
    """Return True iff ``to_state`` is an allowed transition from ``from_state``.

    Pure and deterministic: the same pair always yields the same answer (§15). Accepts either the
    enum member or its stored string value so callers can pass raw column data directly.
    """
    # An unknown token must be treated as an invalid move, never leak the enum's ValueError.
    try:
        fs, ts = PublicationStatus(from_state), PublicationStatus(to_state)
    except ValueError:
        return False
    return (fs, ts) in VALID_TRANSITIONS


class PublicationStateMachine:
    """Applies and validates :class:`PublicationStatus` transitions for a single publication row."""

    def __init__(self, publication) -> None:
        self.publication = publication

    @property
    def state(self) -> str:
        return str(self.publication.status)

    def transition_to(self, new_state: str | PublicationStatus) -> bool:
        """Apply ``new_state`` if valid; otherwise raise :class:`InvalidTransitionError`.

        Returns True when the transition was applied. Raises on invalid moves so a caller can never
        leave a publication in an inconsistent state (§24).
        """
        # Accept either the enum member or a raw stored string. Resolve via ``.value`` first so an
        # unknown token is reported as an invalid *transition* (InvalidTransitionError) rather than
        # leaking the enum's own ValueError from its missing-value handler.
        new_value = new_state.value if isinstance(new_state, PublicationStatus) else str(new_state)
        if not can_transition(self.state, new_value):
            raise InvalidTransitionError(
                f"invalid publication transition: {self.state} -> {new_value}"
            )
        self.publication.status = new_value
        return True

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PublicationStateMachine status={self.state!r}>"
