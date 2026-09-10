"""P5 — Internal persistent destination (section 24).

This destination is the REAL, persistent output channel of NewsForge: when the publisher
delivers an approved publication to it, the content is materialised as a ``articles`` row
(``status=PUBLISHED``) readable at ``/articles/{slug}``. It is idempotent — re-publishing the
same story updates the SAME row (UNIQUE by story_id) instead of duplicating it — and it never
touches the editorial verdict tables (§24). The publisher calls this with the persisted decision
already consumed; this channel does not re-derive trust/quality/risk (§11).

Failure isolation: any expected failure (missing story/artifact, DB error) returns a
:class:`DistributionOutcome` with ``succeeded=False`` so the publisher records a FAILED attempt
and continues with the other channels. Nothing sensitive is ever echoed back in the outcome.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import or_


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


class InternalDestination(Destination):
    """Persist an approved publication as a readable ``articles`` row (P5 gate).

    When the publisher delivers a payload for a story, this channel materialises the
    article into the persistent ``articles`` table with ``status=PUBLISHED`` so it is
    served at ``/articles/{slug}``. Idempotent: the row is keyed by ``story_id`` (UNIQUE),
    re-publishing updates it in place — it never creates duplicates. Content is sourced
    ONLY from already-persisted editorial state (story + generated artifact); no facts are
    invented here. Trust/quality/risk are not consulted: the publisher already consumed
    the Decision Engine verdict before calling publish (§11/§24).
    """

    key = "internal"
    name = "Internal persistent destination"
    type = "WEBSITE"

    def __init__(self, *, payload: dict | None = None) -> None:
        super().__init__(payload=payload)

    def publish(self, payload: dict | None = None) -> DistributionOutcome:
        from newsforge.db.models import (
            ArticleStatus,
            articles,
            generated_artifacts,
            from_jsonable,
            stories,
            to_jsonable,
        )
        from newsforge.db.session import get_session

        data = dict(payload or self.payload or {})
        story_ref = str(data.get("story_id") or "")
        if not story_ref:
            return DistributionOutcome(succeeded=False, error="missing story_id in payload")

        try:
            with get_session() as session:
                story = session.query(stories).filter(
                    or_(stories.id == story_ref, stories.story_id == story_ref)
                ).first()
                if story is None:
                    return DistributionOutcome(succeeded=False, error=f"story {story_ref!r} not found")
                slug = story.slug or story.story_id
                title = story.title or slug

                artifact = (session.query(generated_artifacts)
                            .filter_by(story_id=str(story.id))
                            .order_by(generated_artifacts.created_at.desc()).first())

                body = from_jsonable(artifact.body_json) if artifact is not None else None
                sections = (body or {}).get("sections") if isinstance(body, dict) else None
                body_html = _sections_to_html(sections) if sections else (story.summary or "")
                words = sum(len(str(s.get("text", "")).split()) for s in (sections or []))
                read_time = max(1, round(words / 200.0)) if words else 0

                existing = session.query(articles).filter_by(story_id=str(story.id)).first()
                if existing is None:
                    row = articles(
                        story_id=str(story.id),
                        title=title,
                        slug=slug,
                        content_type="NEWS",
                        language="es",
                        facts_json=to_jsonable({"sections": sections} if sections else None),
                        body_html=body_html,
                        meta_description=(story.summary or "")[:200] or None,
                        seo_title=title,
                        word_count=words,
                        read_time_min=read_time,
                        status=ArticleStatus.PUBLISHED.value,
                        published_at=data.get("published_at") or None,
                    )
                    session.add(row)
                else:
                    existing.title = title
                    existing.slug = slug
                    existing.facts_json = to_jsonable({"sections": sections} if sections else None)
                    existing.body_html = body_html
                    existing.meta_description = (story.summary or "")[:200] or None
                    existing.seo_title = title
                    existing.word_count = words
                    existing.read_time_min = read_time
                    existing.status = ArticleStatus.PUBLISHED.value
                    if data.get("published_at") and not existing.published_at:
                        existing.published_at = data["published_at"]
                session.commit()
        except Exception as exc:  # noqa: BLE001 - isolate ANY failure to this channel (§24)
            return DistributionOutcome(
                succeeded=False, error=f"{type(exc).__name__}: {exc}"
            )

        return DistributionOutcome(
            succeeded=True,
            published_at=data.get("published_at") or None,
            payload=data,
        )


def _sections_to_html(sections) -> str:
    """Render artifact sections to a minimal safe HTML fragment (read-only derivation).

    Only text content already present in the persisted artifact is used; nothing new is
    authored. Paragraphs are escaped so the stored HTML can never carry injected markup."""
    from html import escape

    parts = []
    for s in sections or []:
        text = str(s.get("text", "") or "").strip()
        if not text:
            continue
        tag = {
            "bullet": "li", "fact": "p", "intro": "p", "note": "p", "post": "p",
            "greeting": "p", "footer": "p", "vo_intro": "p", "vo_fact": "p",
            "vo_outro": "p", "event": "p", "qa": "p", "question": "p", "answer": "p",
        }.get(str(s.get("type", "")), "p")
        if tag == "li":
            parts.append(f"<li>{escape(text)}</li>")
        else:
            parts.append(f"<{tag}>{escape(text)}</{tag}>")
    return "".join(parts)


def register_builtin_destinations() -> None:
    """Register the built-in destinations so they are available to the publisher.

    ``internal`` is the REAL persistent channel (P5); ``recording`` remains a test/demo
    stub. Both are entirely offline and safe to enable by default."""
    if not any(k == "recording" for k in _REGISTRY):
        register("recording", RecordingDestination)
    if not any(k == "internal" for k in _REGISTRY):
        register("internal", InternalDestination)
