"""Source trust scoring (§6, §7).

Every source carries a *tier* (primary/official → social/rumor) and a stored
``trust_score``. This module derives:

- the baseline trust from the tier,
- a recency penalty for stale health-checks,
- an item-level confidence that combines source trust with freshness.

All functions are pure so they can be unit-tested deterministically.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from newsforge.db.models import SourceTier

# Baseline reliability per tier (§7). Higher = more trustworthy seed material.
TIER_BASE_TRUST: dict[str, int] = {
    SourceTier.TIER_1.value: 95,   # primary / official (BOE, EU, gov, scientific bodies)
    SourceTier.TIER_2.value: 80,   # highly reliable journalism
    SourceTier.TIER_3.value: 60,   # secondary outlets
    SourceTier.TIER_4.value: 35,   # social / rumor / unverified — never auto-publishes
}

# A source not health-checked in > 30 days is considered stale.
STALE_AFTER_DAYS = 30


def clamp(value: float, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, int(round(value))))


def tier_baseline(tier: str | None) -> int:
    if not tier:
        return 50
    return TIER_BASE_TRUST.get(str(tier).upper(), 50)


def _days_between(a: datetime | str | None, b: datetime | None = None) -> float:
    now = datetime.now(timezone.utc)
    a_dt = (datetime.fromisoformat(a.replace("Z", "+00:00")) if isinstance(a, str) else a) or now
    b_dt = b or now
    return (b_dt - a_dt).total_seconds() / 86400.0


def source_trust_score(tier: str | None, last_checked: str | None = None) -> int:
    """Composite trust for a *source* from its tier and health-check recency."""
    score = tier_baseline(tier)
    if last_checked:
        age_days = _days_between(last_checked)
        if age_days > STALE_AFTER_DAYS:
            score -= 20
    return clamp(score)


def freshness_factor(days_old: float | None) -> float:
    """Mild confidence decay for very old items (freshness matters less over time)."""
    if days_old is None or days_old < 1:
        return 1.0
    # Linear decay from 1.0 at 1 day to 0.75 at 30+ days.
    return max(0.75, 1.0 - (days_old - 1) / 60.0)


def item_confidence(source_trust_score: int, published_at: str | None = None) -> int:
    """Confidence (0-100) attributed to a single ingested item."""
    days_old = _days_between(published_at) if published_at else 0.0
    factor = freshness_factor(days_old)
    return clamp(source_trust_score * factor)


# --- anti-slop helpers (§50) ------------------------------------------------- #

_GENERIC_PHRASES = (
    "in today's fast-changing world", "it goes without saying", "as we all know",
    "when it comes to", "at the end of the day", "in recent years", "over the past few",
    "it is important to note", "worth noting that", "according to experts", "many people believe",
)


def generic_phrase_ratio(text: str | None) -> float:
    """Fraction of sentences that are near-verbatim generic filler phrases."""
    if not text:
        return 0.0
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    if len(normalized) < 24:
        return 0.0
    ratio = 0.0
    count = 0
    for phrase in _GENERIC_PHRASES:
        occurrences = normalized.count(phrase)
        count += occurrences
        ratio += occurrences / max(1, len(normalized.split()))
    # Normalize to a 0-1 ratio relative to the number of generic phrases.
    return min(1.0, (count * 3) / max(1, len(_GENERIC_PHRASES)))
