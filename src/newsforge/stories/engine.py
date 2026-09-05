"""Story Engine — persist detected stories and link their source items (§4).

The :class:`StoryDetector` turns ingested *signals* (rows in ``source_items``) into
persistent stories. Detection is deterministic: items are grouped by ``(topic, year)``
and a shared entity prefix disambiguates them, producing stable ``STORY_ID``s. The same
item set always yields the same story ids, which keeps :meth:`StoryDetector.process`
idempotent across repeated runs (important for an autonomous pipeline that re-detects on
every source check).

Trust aggregation here is a deterministic MVP proxy (§9): each contributing item's trust
is its source *tier baseline* and the story score is their mean. Freshness/consensus/
primary-source weighting are layered on in later phases (P3 Trust Engine) without changing
this API — only :meth:`StoryDetector._item_score` would change.

All detection logic lives in :mod:`newsforge.stories.detector`; this module owns the DB
side effects (upsert + link).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from sqlalchemy.orm import Session

from newsforge.core.logger import get_logger
from newsforge.db.models import SourceTier, StoryStatus, stories, story_signals, source_items, sources
from newsforge.db.session import get_session
from newsforge.sources.trust import tier_baseline
from newsforge.stories.detector import cluster_items, classify_item, slugify

logger = get_logger("stories.engine")

# How long a synthesized (verbatim-from-sources) summary is allowed to grow.
SUMMARY_MAX_CHARS = 500


@dataclass
class ProcessResult:
    """Summary of what :meth:`StoryDetector.process` persisted."""

    created_stories: int = 0
    updated_stories: int = 0
    linked_signals: int = 0
    stories: list[dict[str, Any]] = field(default_factory=list)


class StoryDetector:
    """Detect and persist persistent stories from ingested signals (§4)."""

    # ------------------------------------------------------------------ #
    # Pure detection helpers (thin wrappers over the deterministic core)
    # ------------------------------------------------------------------ #
    @staticmethod
    def detect(items: Sequence[dict]) -> dict[str, list[dict]]:
        """Cluster items into stories keyed by STORY_ID (pure; no DB)."""
        from newsforge.stories.detector import cluster_items

        return cluster_items(list(items))

    @staticmethod
    def classify_item(item: dict) -> dict[str, str | None]:
        """Return the natural story key for a single item."""
        from newsforge.stories.detector import classify_item as _classify_item

        return _classify_item(item)

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def process(
        self,
        *,
        signal_ids: Iterable[str] | None = None,
        signals: Sequence[dict] | None = None,
    ) -> ProcessResult:
        """Detect stories from ``signals`` (or DB rows identified by ``signal_ids``) and persist them.

        Idempotent: re-running with the same input creates no duplicate stories or links
        and recomputes trust deterministically. Returns a :class:`ProcessResult`.
        """
        if signals is None and signal_ids is None:
            raise ValueError("process() requires either `signals` or `signal_ids`.")

        # Use a single session for the whole operation. Opening/closing many short-lived
        # sessions on Windows+SQLite (QueuePool) can leak connections and make commits
        # unreliable; one session per logical step is both faster and more robust (§42).
        with get_session() as session:
            items = self._resolve_items(session, signal_ids, signals)
            clusters = self.detect(items)

            result = ProcessResult()
            for story_id, group in clusters.items():
                created, updated, linked = self._upsert_story(session, story_id, group)
                if created:
                    result.created_stories += 1
                elif updated:
                    result.updated_stories += 1
                result.linked_signals += linked
                result.stories.append(self._story_view(session, story_id))

            session.commit()  # persist stories + links so later sessions see them

        logger.info(
            "Story detection: +%d stories, ~%d updated, %d signals linked",
            result.created_stories, result.updated_stories, result.linked_signals,
        )
        return result

    # ------------------------------------------------------------------ #
    # Internal steps
    # ------------------------------------------------------------------ #
    def _story_view(self, session: Session, story_id: str) -> dict[str, Any]:
        """Build a public-facing summary of a persisted story."""
        story = session.query(stories).filter_by(story_id=story_id).first()
        if story is None:
            return {"story_id": story_id}
        signal_count = len(session.query(story_signals).filter_by(story_id=story_id).all())
        return {
            "story_id": story.story_id,
            "title": story.title,
            "slug": story.slug,
            "topic": story.topic,
            "status": story.status,
            "trust_score": story.trust_score,
            "signal_count": signal_count,
        }

    def _resolve_items(self, session: Session, signal_ids: Iterable[str] | None, signals: Sequence[dict]) -> list[dict]:
        """Return normalized item dicts (with an ``id`` and optional ``trust`` hint)."""
        if signals is not None:
            return [self._normalize_signal(s) for s in signals]

        ids = list(signal_ids or [])
        rows = []
        # Accept UUID PKs (contain a dash) and dedupe hashes. Only apply each filter
        # when it has values — an empty IN () would match nothing.
        uuid_keys, hash_keys = [], []
        for i in ids:
            key = str(i).strip().lower()
            (uuid_keys if "-" in key else hash_keys).append(key)
        conds = [source_items.id.in_(uuid_keys)] if uuid_keys else []
        conds += [source_items.dedupe_hash.in_(hash_keys)] if hash_keys else []
        rows = session.query(source_items).filter(*conds).all()
        return [self._normalize_row(r) for r in rows]

    @staticmethod
    def _normalize_signal(sig: dict) -> dict[str, Any]:
        item_id = sig.get("id") or sig.get("dedupe_hash")
        if not item_id:
            raise ValueError("signal requires an 'id' (source_item UUID) to be persisted.")
        return {
            "id": str(item_id),
            "title": sig.get("title"),
            "description": sig.get("description"),
            "content_text": sig.get("content_text") or "",
            "published_at": sig.get("published_at"),
            "trust": float(sig.get("trust", 50.0)),
        }

    @staticmethod
    def _normalize_row(row: source_items) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "title": row.title,
            "description": row.description,
            "content_text": row.content_text or "",
            "published_at": row.published_at,
            "trust": 50.0,  # refined from source tier in _upsert_story
        }

    def _item_score(self, item: dict, session: Session | None = None) -> float:
        """Deterministic MVP trust proxy for one contributing item (§9)."""
        if not item.get("id"):
            return float(item.get("trust", 50.0))
        # `sources.id` is the PK; look up by the indexed, unique `source_id` FK column.
        if session is not None:
            row = session.get(source_items, item["id"])
            src = (row or None) and session.query(sources).filter_by(source_id=row.source_id).first()
            if src is not None:
                return float(tier_baseline(src.tier))
        else:
            with get_session() as sess:
                row = sess.get(source_items, item["id"])
                src = (row or None) and sess.query(sources).filter_by(source_id=row.source_id).first()
                if src is not None:
                    return float(tier_baseline(src.tier))
        # Fallback for raw signals without a backing source row.
        return float(item.get("trust", 50.0))

    def _upsert_story(self, session: Session, story_id: str, group: list[dict]) -> tuple[bool, bool, int]:
        """Upsert one story and link its items. Returns ``(created, updated, linked)``."""
        existing = session.query(stories).filter_by(story_id=story_id).first()

        # Link items first (idempotent via the unique constraint), counting new links.
        already_linked = {str(s.item_id) for s in session.query(story_signals).filter_by(story_id=story_id).all()}
        linked = 0
        for item in group:
            iid = str(item["id"])
            if iid not in already_linked:
                link = story_signals()
                link.story_id, link.item_id = story_id, iid
                session.add(link)
                already_linked.add(iid)
                linked += 1

        # Aggregate trust from contributing items' source tiers (§9 MVP proxy).
        scores = [self._item_score(item, session) for item in group] or [50.0]
        trust = round(sum(scores) / len(scores))

        if existing is None:
            title = self._latest_title(group)
            topic_slug = self.classify_item(group[0])["topic_slug"] if group else None
            story = stories()
            story.story_id = story_id
            story.title = title
            story.slug = slugify(title) or story_id
            story.summary = self._summarize(group)
            story.topic = topic_slug
            story.status = StoryStatus.ACTIVE.value
            story.trust_score = trust
            session.add(story)  # UUID pk assigned by the column default
            return True, False, linked

        # Existing story — refresh metadata + links and recompute trust.
        title = self._latest_title(group)
        topic_slug = self.classify_item(group[0])["topic_slug"] if group else None
        existing.title = title or existing.title
        existing.slug = slugify(title) or existing.slug or story_id
        existing.summary = self._summarize(group) or existing.summary
        existing.topic = topic_slug
        existing.trust_score = trust
        session.flush()  # assign the UUID pk so nothing downstream needs it
        return False, True, linked

    @staticmethod
    def _latest_title(group: list[dict]) -> str | None:
        """Title of the most recently published item (directly sourced — never invented)."""
        ordered = sorted(group, key=lambda i: (i.get("published_at") or ""), reverse=True)
        for item in ordered:
            if item.get("title"):
                return item["title"]
        return None

    @staticmethod
    def _summarize(group: list[dict]) -> str | None:
        """Factual, de-duplicated summary built only from source text (§51 originality)."""
        seen: set[str] = set()
        parts: list[str] = []
        for item in sorted(group, key=lambda i: (i.get("published_at") or ""), reverse=True):
            desc = (item.get("description") or "").strip()
            if not desc:
                continue
            for sentence in _split_sentences(desc)[:2]:
                key = sentence.strip().lower()
                if key and key not in seen:
                    seen.add(key)
                    parts.append(sentence.strip())
        summary = " ".join(parts).strip()
        return summary[:SUMMARY_MAX_CHARS] if summary else None


def _split_sentences(text: str) -> list[str]:
    """Split on sentence terminators, keeping trailing punctuation."""
    pieces = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in pieces if p]
