"""Distribution destinations (section 24).

A destination is a single output channel for published content: website, RSS, newsletter, social,
feeds/API, etc. The design goal (§24) is that a failure in one destination must NOT corrupt the
global state of the Story nor prevent other destinations from publishing. Each destination is
therefore attempted independently and its result recorded separately in ``publication_attempts``.

This module ships ONLY record-only stubs for the MVP: no network access, no external dependencies.
A production destination would implement :meth:`Destination.publish` against a real channel; the
interface here makes that drop-in without touching the publisher's safety guarantees. Nothing in
this module touches trust/quality/risk scoring -- those are computed once by the Decision Engine and
consumed as a persisted verdict by the publisher (:func:`newsforge.publish.publisher.is_auto_publishable`).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class DistributionOutcome:
    """Result of a single destination publish attempt.

    ``succeeded`` is authoritative; ``error`` carries the reason on failure and ``payload`` echoes
    what was handed to the channel for auditability. This stays deterministic: the same destination
    given the same payload yields the same outcome (§15).
    """

    succeeded: bool
    error: str | None = None
    published_at: str | None = None
    payload: dict | None = None

    @property
    def ok(self) -> bool:
        return self.succeeded


class Destination(ABC):
    """An output channel. Concrete destinations implement :meth:`publish` and are registered by key."""

    #: Stable, human-readable identifier used as the publication's destination_key.
    key: str = ""
    #: Display name surfaced in audit logs.
    name: str = ""
    #: Canonical type from :class:`newsforge.db.models.DestinationType`.
    type: str = ""

    def __init__(self, *, payload: dict | None = None) -> None:
        self.payload = payload or {}

    @abstractmethod
    def publish(self, payload: dict | None = None) -> DistributionOutcome:
        """Publish ``payload`` to this channel.

        Must be deterministic and must NOT raise for expected transient failures; instead return a
        :class:`DistributionOutcome` with ``succeeded=False`` so the publisher can isolate the failure
        and continue with other destinations (§24). Raises only on unexpected/programming errors.
        """
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Registry -- the single source of truth for available destination keys.
# --------------------------------------------------------------------------- #
_REGISTRY: dict[str, type[Destination]] = {}


def register(key: str, cls: type[Destination]) -> None:
    """Register a destination *class* under ``key`` so it is discovered by the publisher."""
    _REGISTRY[key] = cls


def available_keys() -> list[str]:
    """Return the sorted set of registered destination keys (deterministic order)."""
    return sorted(_REGISTRY)


def get_destination(key: str, *, payload: dict | None = None) -> Destination | None:
    """Instantiate a registered destination by key, or return None if unknown."""
    cls = _REGISTRY.get(key)
    if cls is None:
        return None
    return cls(payload=payload)


def reset_registry() -> None:  # pragma: no cover - test helper to clear global registrations
    _REGISTRY.clear()


# --------------------------------------------------------------------------- #
# Built-in MVP destinations (record-only, no network).
# --------------------------------------------------------------------------- #
class RecordingDestination(Destination):
    """In-memory destination that records every publish call and reports success or failure.

    Used for tests and offline demos. It performs NO I/O: the outcome is fully controlled by the
    ``should_succeed`` flag (or by raising :class:`Exception` via ``raise_on_publish``) so behaviour
    is deterministic (§15). A real channel would go here in production; the publisher never assumes
    what a destination does internally.
    """

    def __init__(self, *, key: str = "recording", name: str = "Recording Destination",
                 type: str = "GENERIC", should_succeed: bool = True,
                 raise_on_publish: Exception | None = None, payload: dict | None = None) -> None:
        super().__init__(payload=payload)
        self.key = key
        self.name = name
        self.type = type
        self.should_succeed = should_succeed
        self.raise_on_publish = raise_on_publish
        self.calls = []  # instance-level list; each destination records its own publish calls

    def publish(self, payload: dict | None = None) -> DistributionOutcome:
        payload = dict(payload or self.payload or {})
        self.calls.append({"payload": payload})
        if self.raise_on_publish is not None:
            raise self.raise_on_publish
        if not self.should_succeed:
            return DistributionOutcome(succeeded=False, error="recorded failure")
        return DistributionOutcome(succeeded=True, published_at="2026-09-07T00:00:00+00:00", payload=payload)


def register_builtin_destinations() -> None:
    """Register the built-in record-only destinations so they are available to the publisher."""
    if not any(k == "recording" for k in _REGISTRY):
        register("recording", RecordingDestination)
