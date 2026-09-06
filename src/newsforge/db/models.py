"""NewsForge data model — entities from the brief §34.

Design notes:
- UUID primary keys everywhere (safe for concurrent ingestion).
- Enums stored as String columns -> portable across SQLite/PostgreSQL without
  dialect-specific serialization quirks; validated in Python at the API layer.
- JSON blobs use module helpers :func:`to_jsonable` / :func:`from_jsonable`.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from sqlalchemy import BigInteger, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import (
    Base,
    json_col,
    ts,
    ts_col,
    ts_nullable,
    uuid_pk,
)


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #
def to_jsonable(obj: Any) -> str | None:
    if obj is None or obj == "":
        return None
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None


def from_jsonable(value: Optional[str], default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class ClaimStatus(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    CONTRADICTED = "CONTRADICTED"
    OUTDATED = "OUTDATED"


class ArticleStatus(str, Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"
    REJECTED = "REJECTED"


class StoryStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"
    ARCHIVED = "ARCHIVED"


class DecisionState(str, Enum):
    """CONTENT_DECISION_ENGINE states (§16)."""
    REJECT = "REJECT"
    WAIT = "WAIT"
    DRAFT = "DRAFT"
    REVIEW = "REVIEW"
    PUBLISH = "PUBLISH"
    UPDATE = "UPDATE"
    ARCHIVE = "ARCHIVE"


class HumanLoopVerdict(str, Enum):
    GREEN = "GREEN"          # auto-publish
    YELLOW = "YELLOW"        # review recommended
    RED = "RED"              # mandatory human review


class ContentType(str, Enum):
    NEWS = "NEWS"
    BREAKING = "BREAKING"
    EXPLAINER = "EXPLAINER"
    ANALYSIS = "ANALYSIS"
    GUIDE = "GUIDE"
    COMPARISON = "COMPARISON"
    DATA = "DATA"
    FAQ = "FAQ"
    REVIEW = "REVIEW"
    INVESTIGATION = "INVESTIGATION"
    TIMELINE = "TIMELINE"
    LIVE_UPDATE = "LIVE_UPDATE"
    TOOL = "TOOL"
    CALCULATOR = "CALCULATOR"
    DATABASE = "DATABASE"
    REPORT = "REPORT"


class SourceTier(str, Enum):
    TIER_1 = "TIER_1"          # primary / official
    TIER_2 = "TIER_2"          # highly reliable journalism
    TIER_3 = "TIER_3"          # secondary
    TIER_4 = "TIER_4"          # social / rumor / unverified


class SourceType(str, Enum):
    RSS = "RSS"
    API = "API"
    OFFICIAL = "OFFICIAL"      # gov / BOE / EU / scientific bodies
    SCIENTIFIC = "SCIENTIFIC"
    PRESS_RELEASE = "PRESS_RELEASE"
    WEBSITE = "WEBSITE"


class AiTaskType(str, Enum):
    DISCOVER = "DISCOVER"
    INGEST = "INGEST"
    NORMALIZE = "NORMALIZE"
    DEDUPLICATE = "DEDUPLICATE"
    CLASSIFY = "CLASSIFY"
    CLUSTER = "CLUSTER"
    CREATE_STORY = "CREATE_STORY"
    VERIFY = "VERIFY"
    DECIDE = "DECIDE"
    GENERATE = "GENERATE"
    QUALITY_CONTROL = "QUALITY_CONTROL"
    SEO = "SEO"
    DISTRIBUTE = "DISTRIBUTE"
    MEASURE = "MEASURE"
    OPTIMIZE = "OPTIMIZE"


class AiJobStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"


# --------------------------------------------------------------------------- #
# Core tables
# --------------------------------------------------------------------------- #
class users(Base):
    __tablename__ = "users"

    id: Mapped[str] = uuid_pk()
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="viewer", nullable=False)
    password_hash: Mapped[bytes | None] = mapped_column(Text)
    salt: Mapped[str | None] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[str] = ts_col()


class authors(Base):
    __tablename__ = "authors"

    id: Mapped[str] = uuid_pk()
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    bio: Mapped[str | None] = mapped_column(Text)


class sources(Base):
    __tablename__ = "sources"

    id: Mapped[str] = uuid_pk()
    source_id: Mapped[str] = mapped_column(String(128), default=lambda: str(uuid.uuid4()), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2048))
    type: Mapped[str] = mapped_column(String(40), default=SourceType.WEBSITE.value, nullable=False)
    country: Mapped[str | None] = mapped_column(String(64))
    language: Mapped[str] = mapped_column(String(8), default="es", nullable=False)
    tier: Mapped[str] = mapped_column(String(16), default=SourceTier.TIER_3.value, nullable=False)
    trust_score: Mapped[int] = mapped_column(BigInteger, default=50, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    last_checked: Mapped[str | None] = ts_nullable()


class source_items(Base):
    __tablename__ = "source_items"

    id: Mapped[str] = uuid_pk()
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(2048))
    description: Mapped[str | None] = mapped_column(Text)
    content_html: Mapped[str | None] = mapped_column(Text)
    content_text: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[str | None] = ts_nullable()
    fetched_at: Mapped[str] = ts_col()
    dedupe_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    raw_json: Mapped[str | None] = json_col()


# --------------------------------------------------------------------------- #
# Story Engine (§4) — link detected source items to their persistent story
# --------------------------------------------------------------------------- #
class story_signals(Base):
    """Many-to-many join linking a detected *story* (§4) to its source items (§6).

    One row per ``(story_id, item_id)`` pair. This is the canonical link that lets the
    Story Engine update an existing story when a related signal arrives, and lets
    analytics count how many signals feed each story. The unique constraint keeps
    re-detection idempotent.
    """
    __tablename__ = "story_signals"

    id: Mapped[str] = uuid_pk()
    story_id: Mapped[str] = mapped_column(String(128), ForeignKey("stories.id"), index=True, nullable=False)
    item_id: Mapped[str] = mapped_column(String(36), ForeignKey("source_items.id"), index=True, nullable=False)
    created_at: Mapped[str] = ts_col()

    story: Mapped["stories"] = relationship(back_populates="signals", lazy="select")
    source_item: Mapped[source_items] = relationship(lazy="selectin")


class stories(Base):
    __tablename__ = "stories"


    id: Mapped[str] = uuid_pk()
    story_id: Mapped[str] = mapped_column(String(128), default=lambda: str(uuid.uuid4()), unique=True, index=True, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(String(300), index=True, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    topic: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(20), default=StoryStatus.ACTIVE.value, nullable=False)
    trust_score: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[str] = ts_col()
    updated_at: Mapped[str] = ts_col(onupdate=ts)

    articles: Mapped[list["articles"]] = relationship(
        back_populates="story", cascade="all, delete-orphan")

    signals: Mapped[list["story_signals"]] = relationship(back_populates="story", lazy="select")


class articles(Base):
    __tablename__ = "articles"

    id: Mapped[str] = uuid_pk()
    article_id: Mapped[str] = mapped_column(String(128), default=lambda: str(uuid.uuid4()), unique=True, index=True, nullable=False)
    story_id: Mapped[str | None] = mapped_column(ForeignKey("stories.id"), index=True, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(300), index=True, nullable=False)
    content_type: Mapped[str] = mapped_column(String(40), default=ContentType.NEWS.value, nullable=False)
    language: Mapped[str] = mapped_column(String(8), default="es", nullable=False)

    # Facts / analysis / opinion are separated explicitly (§12).
    facts_json: Mapped[str | None] = json_col()
    analysis_json: Mapped[str | None] = json_col()
    opinion_json: Mapped[str | None] = json_col()

    body_html: Mapped[str | None] = mapped_column(Text)
    meta_description: Mapped[str | None] = mapped_column(Text)
    seo_title: Mapped[str | None] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(default=0, nullable=False)
    read_time_min: Mapped[int] = mapped_column(default=0, nullable=False)

    status: Mapped[str] = mapped_column(String(20), default=ArticleStatus.DRAFT.value, nullable=False)
    published_at: Mapped[str | None] = ts_nullable()
    created_at: Mapped[str] = ts_col()
    updated_at: Mapped[str] = ts_col(onupdate=ts)

    story: Mapped["stories"] = relationship(back_populates="articles", lazy="select")


class claims(Base):
    __tablename__ = "claims"

    id: Mapped[str] = uuid_pk()
    # Unique so re-detecting the same claim is idempotent (upsert) rather than duplicated (§17, Case 10).
    claim_id: Mapped[str] = mapped_column(String(128), default=lambda: str(uuid.uuid4()), unique=True, index=True, nullable=False)
    article_id: Mapped[str | None] = mapped_column(ForeignKey("articles.id"), index=True, nullable=True)
    # Provenance (§2): which STORY and SOURCE ITEM this claim came from.
    story_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    source_item_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2048))
    publication_date: Mapped[str | None] = ts_nullable()
    verified_at: Mapped[str | None] = ts_nullable()
    confidence: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)  # 0-100
    status: Mapped[str] = mapped_column(String(32), default=ClaimStatus.UNVERIFIED.value, nullable=False)


class fact_checks(Base):
    __tablename__ = "fact_checks"

    id: Mapped[str] = uuid_pk()
    claim_id: Mapped[str | None] = mapped_column(ForeignKey("claims.id"), index=True, nullable=True)
    result: Mapped[str] = mapped_column(String(20), nullable=False)  # TRUE / FALSE / UNCLEAR
    evidence_text: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[str] = ts_col()
    checker: Mapped[str | None] = mapped_column(String(128))


# --------------------------------------------------------------------------- #
# Verification & decisions (§1, §2, §6–§10) — new in P3
# --------------------------------------------------------------------------- #
class claim_evidence(Base):
    """Independent evidence linking a CLAIM to the SOURCE ITEM that supports it (§3).

    One row per (claim, source_item) pair. The unique constraint makes re-detection
    idempotent and lets corroboration count DISTINCT sources: two rows from the same
    underlying source do NOT count as independent support.
    """
    __tablename__ = "claim_evidence"

    id: Mapped[str] = uuid_pk()
    claim_id: Mapped[str] = mapped_column(String(128), ForeignKey("claims.id"), index=True, nullable=False)
    source_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("source_items.id"), index=True, nullable=False)
    created_at: Mapped[str] = ts_col()

    __table_args__ = (
        UniqueConstraint("claim_id", "source_item_id", name="uq_claim_evidence_claim_source"),
    )


class trust_evaluations(Base):
    """Structured, explainable TRUST score for a STORY or CLAIM (§6, §7).

    The score is never opaque: factors_json records the positive/negative reasons so an
    auditor can see exactly what drove the number. policy_version keeps evaluations
    traceable to the rule set that produced them (§18).
    """
    __tablename__ = "trust_evaluations"

    id: Mapped[str] = uuid_pk()
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)  # STORY / CLAIM
    target_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    trust_score: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    source_trust: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    independent_corroboration: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_evidence: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    contradiction_penalty: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    freshness_score: Mapped[float] = mapped_column(default=0.0, nullable=False)
    risk_level: Mapped[str | None] = mapped_column(String(16))  # GREEN/YELLOW/ORANGE/RED
    factors_json: Mapped[str | None] = json_col()
    policy_version: Mapped[str] = mapped_column(String(32), default="p3.v1", nullable=False)
    computed_at: Mapped[str] = ts_col()

    # Idempotency (§17): one evaluation per (target_type, target_id). Re-inserting the
    # same logical evaluation raises IntegrityError and is treated as a no-op by callers.
    __table_args__ = (
        UniqueConstraint("target_type", "target_id", name="uq_trust_evaluations_target"),
    )


class quality_evaluations(Base):
    """QUALITY GATE result for a STORY or CLAIM (§9).

    passed=False means the content must not be auto-published. reasons_json is structured
    (not free text) so failures can be filtered, reported and audited deterministically.
    """
    __tablename__ = "quality_evaluations"

    id: Mapped[str] = uuid_pk()
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)  # STORY / CLAIM
    target_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    passed: Mapped[bool] = mapped_column(default=False, nullable=False)
    score: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)  # 0-100 composite
    reasons_json: Mapped[str | None] = json_col()
    policy_version: Mapped[str] = mapped_column(String(32), default="p3.v1", nullable=False)
    computed_at: Mapped[str] = ts_col()

    # Idempotency (§17): one evaluation per (target_type, target_id). Re-inserting the
    # same logical evaluation raises IntegrityError and is treated as a no-op by callers.
    __table_args__ = (
        UniqueConstraint("target_type", "target_id", name="uq_quality_evaluations_target"),
    )


class decisions(Base):
    """CONTENT DECISION ENGINE state-machine record (§10, §14).

    Upserted by (target_type, target_id) so re-running the same evaluation is idempotent.
    reasons_json + policy_version make every decision auditable: what was decided, why,
    on which data, and under which rule version. human_override records when a reviewer
    overrode the system's recommendation.
    """
    __tablename__ = "decisions"

    id: Mapped[str] = uuid_pk()
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)  # STORY / CLAIM
    target_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    decision: Mapped[str] = mapped_column(String(20), default=DecisionState.PUBLISH.value, nullable=False)
    risk_level: Mapped[str | None] = mapped_column(String(16))  # GREEN/YELLOW/ORANGE/RED
    trust_score: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reasons_json: Mapped[str | None] = json_col()
    human_override: Mapped[bool] = mapped_column(default=False, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), default="p3.v1", nullable=False)
    created_at: Mapped[str] = ts_col()
    updated_at: Mapped[str] = ts_col(onupdate=ts)

    __table_args__ = (
        UniqueConstraint("target_type", "target_id", name="uq_decisions_target"),
    )


class review_tasks(Base):
    """Human REVIEW QUEUE (§13).

    A decision that cannot auto-publish (REVIEW/WAIT/REJECT) becomes a task with a
    lifecycle: created → assigned → approved / rejected / edited / overridden. The backend
    leaves this structure ready; no UI is built here.
    """
    __tablename__ = "review_tasks"

    id: Mapped[str] = uuid_pk()
    decision_id: Mapped[str] = mapped_column(String(36), ForeignKey("decisions.id"), index=True, nullable=False)
    story_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    claim_ids_json: Mapped[str | None] = json_col()
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)  # PENDING/ASSIGNED/APPROVED/REJECTED/EDITED/OVERRIDDEN
    assigned_to: Mapped[str | None] = mapped_column(String(128))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = ts_col()
    updated_at: Mapped[str] = ts_col(onupdate=ts)


class entities(Base):
    __tablename__ = "entities"

    id: Mapped[str] = uuid_pk()
    entity_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)  # PERSON/COMPANY/LAW...
    language: Mapped[str] = mapped_column(String(8), default="es", nullable=False)


class entity_relationships(Base):
    __tablename__ = "entity_relationships"

    id: Mapped[str] = uuid_pk()
    subject_entity_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), index=True, nullable=False)
    predicate: Mapped[str] = mapped_column(String(64), nullable=False)  # WORKS_FOR / PRODUCES / AFFECTS...
    object_entity_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), index=True, nullable=False)


# --------------------------------------------------------------------------- #
# Products & tools (§19, §47)
# --------------------------------------------------------------------------- #
class products(Base):
    __tablename__ = "products"

    id: Mapped[str] = uuid_pk()
    product_id: Mapped[str] = mapped_column(String(128), default=lambda: str(uuid.uuid4()), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(300), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)  # TOOL/DATABASE/ALERT/NEWSLETTER/REPORT...
    language: Mapped[str] = mapped_column(String(8), default="es", nullable=False)
    story_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)


class prices(Base):
    __tablename__ = "prices"

    id: Mapped[str] = uuid_pk()
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id"), index=True, nullable=False)
    value: Mapped[float] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="EUR", nullable=False)
    as_of_date: Mapped[str] = ts_col()
    source_url: Mapped[str | None] = mapped_column(String(2048))


class laws_regulations(Base):
    __tablename__ = "laws_regulations"

    id: Mapped[str] = uuid_pk()
    law_id: Mapped[str] = mapped_column(String(128), default=lambda: str(uuid.uuid4()), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), default="LAW", nullable=False)  # LAW / REGULATION
    jurisdiction: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str | None] = mapped_column(String(64))
    effective_date: Mapped[str | None] = ts_nullable()
    text_summary: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(2048))


class tools(Base):
    __tablename__ = "tools"

    id: Mapped[str] = uuid_pk()
    tool_id: Mapped[str] = mapped_column(String(128), default=lambda: str(uuid.uuid4()), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(300), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)  # CALCULATOR/COMPARATOR/RADAR/TRACKER/ALERT
    language: Mapped[str] = mapped_column(String(8), default="es", nullable=False)


# --------------------------------------------------------------------------- #
# Distribution & monetization (§25, §21, §22)
# --------------------------------------------------------------------------- #
class newsletters(Base):
    __tablename__ = "newsletters"

    id: Mapped[str] = uuid_pk()
    newsletter_id: Mapped[str] = mapped_column(String(128), default=lambda: str(uuid.uuid4()), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(300), index=True, nullable=False)
    frequency: Mapped[str] = mapped_column(String(20), default="DAILY", nullable=False)  # DAILY/WEEKLY/BREAKING
    topic: Mapped[str | None] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(default=True, nullable=False)


class subscriptions(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = uuid_pk()
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    newsletter_id: Mapped[str | None] = mapped_column(ForeignKey("newsletters.id"), index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)  # ACTIVE/PENDING/CANCELLED


class affiliate_links(Base):
    __tablename__ = "affiliate_links"

    id: Mapped[str] = uuid_pk()
    tool_id: Mapped[str | None] = mapped_column(ForeignKey("tools.id"), nullable=True)
    merchant: Mapped[str] = mapped_column(String(255), nullable=False)
    network: Mapped[str] = mapped_column(String(128), default="generic", nullable=False)
    program: Mapped[str | None] = mapped_column(String(255))
    commission_pct: Mapped[float] = mapped_column(default=0.0, nullable=False)
    price: Mapped[float] = mapped_column(BigInteger, default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="EUR", nullable=False)
    available: Mapped[bool] = mapped_column(default=True, nullable=False)
    language: Mapped[str] = mapped_column(String(8), default="es", nullable=False)


class affiliate_clicks(Base):
    __tablename__ = "affiliate_clicks"

    id: Mapped[str] = uuid_pk()
    affiliate_link_id: Mapped[str | None] = mapped_column(ForeignKey("affiliate_links.id"), index=True, nullable=False)
    user_agent_country: Mapped[str | None] = mapped_column(String(8))
    clicked_at: Mapped[str] = ts_col()
    converted: Mapped[bool | None] = mapped_column(nullable=True)


class ad_slots(Base):
    __tablename__ = "ad_slots"

    id: Mapped[str] = uuid_pk()
    slot_key: Mapped[str] = mapped_column(String(64), index=True, nullable=False)  # TOP/SIDEBAR/ARTICLE_TOP...
    placement: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    fill_type: Mapped[str] = mapped_column(String(32), default="DIRECT", nullable=False)  # DIRECT/PAYPERCLICK/...
    cpm_cpc: Mapped[float] = mapped_column(default=0.0, nullable=False)
    min_bid: Mapped[float] = mapped_column(default=0.0, nullable=False)


# --------------------------------------------------------------------------- #
# Analytics & BI (§28, §48)
# --------------------------------------------------------------------------- #
class analytics(Base):
    __tablename__ = "analytics"

    id: Mapped[str] = uuid_pk()
    entity_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)  # article/story/user...
    metric: Mapped[str] = mapped_column(String(64), nullable=False)  # traffic/users/pageviews/CTR/revenue...
    dimension_value: Mapped[str | None] = mapped_column(String(255))
    value: Mapped[float] = mapped_column(BigInteger, default=0.0, nullable=False)
    recorded_at: Mapped[str] = ts_col()


class content_scores(Base):
    __tablename__ = "content_scores"

    id: Mapped[str] = uuid_pk()
    entity_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    traffic: Mapped[float] = mapped_column(default=0.0, nullable=False)
    engagement: Mapped[float] = mapped_column(default=0.0, nullable=False)
    revenue: Mapped[float] = mapped_column(default=0.0, nullable=False)
    search: Mapped[float] = mapped_column(default=0.0, nullable=False)
    social: Mapped[float] = mapped_column(default=0.0, nullable=False)
    authority: Mapped[float] = mapped_column(default=0.0, nullable=False)
    freshness: Mapped[float] = mapped_column(default=0.0, nullable=False)
    composite: Mapped[float] = mapped_column(default=0.0, nullable=False)  # §29
    computed_at: Mapped[str] = ts_col()


# --------------------------------------------------------------------------- #
# AI cost engine & model router (§30, §31, §32)
# --------------------------------------------------------------------------- #
class ai_jobs(Base):
    __tablename__ = "ai_jobs"

    id: Mapped[str] = uuid_pk()
    task_type: Mapped[str] = mapped_column(String(40), nullable=False)  # AiTaskType
    model_provider: Mapped[str | None] = mapped_column(String(64))
    model_name: Mapped[str | None] = mapped_column(String(128))
    tokens_input: Mapped[int] = mapped_column(default=0, nullable=False)
    tokens_output: Mapped[int] = mapped_column(default=0, nullable=False)
    latency_ms: Mapped[float] = mapped_column(default=0.0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=AiJobStatus.SUCCESS.value, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = ts_col()


class ai_runs(Base):
    __tablename__ = "ai_runs"

    id: Mapped[str] = uuid_pk()
    job_id: Mapped[str | None] = mapped_column(ForeignKey("ai_jobs.id"), index=True, nullable=True)
    run_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    output_ref: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)  # article id produced
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[str] = ts_col()


# --------------------------------------------------------------------------- #
# Experimentation (§23)
# --------------------------------------------------------------------------- #
class experiments(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    variant: Mapped[str] = mapped_column(String(32), default="A", nullable=False)  # A/B...
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(default=0.0, nullable=False)
    n: Mapped[int] = mapped_column(default=0, nullable=False)
    started_at: Mapped[str] = ts_col()
    ended_at: Mapped[str | None] = ts_nullable()


# --------------------------------------------------------------------------- #
# Owned audience (§25, §26)
# --------------------------------------------------------------------------- #
class notifications(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = uuid_pk()
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(40), nullable=False)  # NEWSLETTER/PUSH/SOCIAL/ALERT
    story_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    channel: Mapped[str] = mapped_column(String(20), default="EMAIL", nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    read: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[str] = ts_col()


# --------------------------------------------------------------------------- #
# Governance & observability (§35, §42, §54)
# --------------------------------------------------------------------------- #
class audit_logs(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = uuid_pk()
    actor: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(36), index=True)
    before_json: Mapped[str | None] = json_col()
    after_json: Mapped[str | None] = json_col()
    # Which rule version produced this audit record (§18). Lets an auditor trace a decision back to
    # the exact policy that ran, without duplicating the evaluation rows.
    policy_version: Mapped[str | None] = mapped_column(String(32), default="p3.v1", nullable=False)
    created_at: Mapped[str] = ts_col()


class errors(Base):
    __tablename__ = "errors"

    id: Mapped[str] = uuid_pk()
    module: Mapped[str] = mapped_column(String(128), nullable=False)
    error_type: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context_json: Mapped[str | None] = json_col()
    created_at: Mapped[str] = ts_col()
