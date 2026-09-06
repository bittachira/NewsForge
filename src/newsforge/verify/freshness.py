"""Freshness scoring (§4, §15).

A claim can become stale. Freshness is scored explicitly and deterministically: the same
inputs always yield the same result, and time is injectable so tests never depend on "now".

Different kinds of information have different freshness windows — hard news must be fresh,
laws/regulations last long, health claims decay fast. Windows are configurable via config.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# Default freshness windows in days. Longer window = the claim stays "fresh" longer.
DEFAULT_FRESHNESS_WINDOWS: dict[str, float] = {
    "news": 1.0,       # hard news / breaking — must be fresh
    "breaking": 1.0,
    "financial": 7.0,  # financial claims decay quickly
    "health": 2.0,     # health/medical — very sensitive to staleness
    "policy": 30.0,    # policy / law announcements
    "law": 90.0,       # laws and regulations last long
    "general": 90.0,   # general analysis / background
}

# Info types that never go "stale" (window <= 0): they decay slowly instead.
NEVER_STALE_TYPES = {"evergreen", "reference"}

# Default window used for unknown info types.
DEFAULT_WINDOW_DAYS: float = 30.0


def freshness_windows() -> dict[str, float]:
    """Return the configured freshness windows (overridable by callers/config)."""
    return dict(DEFAULT_FRESHNESS_WINDOWS)


def freshness_score(
    created_at: str | None,
    *,
    info_type: str = "news",
    reference_time: str | None = None,
    windows: dict[str, float] | None = None,
) -> tuple[int, bool]:
    """Score how fresh a claim is.

    Returns ``(score_0_to_100, is_stale)``. ``is_stale`` becomes True once the claim ages
    past its window; the score then decays toward 40 (never below). Fully deterministic —
    pass an explicit ``reference_time`` in tests so "now" never leaks in (§15).
    """
    now = _parse(reference_time) or _utcnow()
    created_dt = _parse(created_at) or now
    age_days = max(0.0, (now - created_dt).total_seconds() / 86400.0)

    info_key = str(info_type).lower()
    windows = windows or freshness_windows()
    window = windows.get(info_key, DEFAULT_WINDOW_DAYS)

    if age_days <= window:
        return 100, False

    if window <= 0 or info_key in NEVER_STALE_TYPES:
        # Never-stale types decay very slowly (one point per year), never flag stale.
        score = max(60, 100 - min(age_days / 365.0 * 100.0, 40))
        return int(round(score)), False

    span = window * 2.0
    decay = min(60.0, (age_days - window) / span * 60.0)
    score = max(40, 100 - decay)
    return int(round(score)), True
