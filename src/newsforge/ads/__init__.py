"""Ad-slot architecture: position-based ad insertion for published articles.

This module provides the infrastructure for inserting advertisement slots into
article pages at configurable positions.  An ``AdProvider`` abstraction allows
swapping the fill mechanism (AdSense, Mediavine, etc.) without changing
``article.html`` -- the provider resolves each position to its ad-unit ID and
renders the appropriate HTML snippet.

Status tracking (strictly separated):
  AD_SLOT_AVAILABLE  -- slot exists and is active in the DB
  AD_PROVIDER_LOADED -- provider class instantiated and is_configured()=True
  AD_IMPRESSION      -- tracked by the provider's own reporting (never invented)
  AD_REVENUE         -- tracked via analytics events (never invented)

Usage::

    from newsforge.ads import insert_ad_slots, register_default_slots, get_provider

    # At startup (optional): populate the ad_slots table with defaults.
    register_default_slots(session)

    # At render time: insert slot markers into the article sections list.
    provider = get_provider()
    sections_with_ads = insert_ad_slots(sections, active_slots=active_slots, provider=provider)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from newsforge.db.models import ad_slots


# --------------------------------------------------------------------------- #
# Slot positions (canonical, order matters)
# --------------------------------------------------------------------------- #
class AdPosition:
    """Well-known insertion points inside an article page."""
    HEADER = "HEADER"
    AFTER_INTRO = "AFTER_INTRO"
    MID_ARTICLE = "MID_ARTICLE"
    BEFORE_RELATED = "BEFORE_RELATED"
    FOOTER = "FOOTER"


DEFAULT_POSITIONS = [
    AdPosition.HEADER,
    AdPosition.AFTER_INTRO,
    AdPosition.MID_ARTICLE,
    AdPosition.BEFORE_RELATED,
    AdPosition.FOOTER,
]


# --------------------------------------------------------------------------- #
# Ad slot rendering
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AdSlot:
    """One ad-slot marker embedded into the sections list."""
    slot_key: str
    placement: str
    fill_type: str = "DIRECT"
    active: bool = True


# --------------------------------------------------------------------------- #
# Ad provider abstraction
# --------------------------------------------------------------------------- #
class AdProvider(ABC):
    """Abstract base for ad-fill providers.

    A provider resolves each AdPosition to an ad-unit ID and renders the
    appropriate HTML snippet.  The template (article.html) never changes --
    only the content of the ``<div class="ad-slot">`` marker changes."""

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True when the provider has valid credentials/unit IDs."""

    @abstractmethod
    def render_slot(self, slot: AdSlot) -> str:
        """Render the HTML for one ad slot (provider-specific markup)."""

    @abstractmethod
    def render_head_script(self) -> str:
        """Return a <script> tag to inject in <head> (empty string if none)."""


class PlaceholderProvider(AdProvider):
    """Default fallback: renders empty <div> markers with data attributes.

    Used when no provider is configured (NEWSFORGE_AD_PROVIDER=none) or when
    the configured provider fails to load."""

    def is_configured(self) -> bool:
        return False

    def render_slot(self, slot: AdSlot) -> str:
        return (
            f'<div class="ad-slot" data-slot="{slot.slot_key}" '
            f'data-placement="{slot.placement}" data-fill="{slot.fill_type}">'
            f'<!-- ad: {slot.slot_key} --></div>'
        )

    def render_head_script(self) -> str:
        return ""


class AdSenseProvider(AdProvider):
    """Google AdSense integration.

    Requires NEWSFORGE_ADSENSE_CLIENT_ID and per-slot unit IDs via
    NEWSFORGE_ADSENSE_SLOT_{POSITION} env vars.  When any required value
    is missing, is_configured() returns False and the placeholder is used."""

    def __init__(self, client_id: str, slot_ids: dict[str, str]):
        self._client_id = client_id
        self._slot_ids = dict(slot_ids)

    def is_configured(self) -> bool:
        if not self._client_id:
            return False
        return any(self._slot_ids.values())

    def render_slot(self, slot: AdSlot) -> str:
        unit_id = self._slot_ids.get(slot.placement, "")
        if not unit_id:
            return PlaceholderProvider().render_slot(slot)
        return (
            f'<div class="ad-slot ad-slot--adsense" '
            f'data-slot="{slot.slot_key}" '
            f'data-placement="{slot.placement}" '
            f'data-fill="ADSENSE" '
            f'data-ad-client="{self._client_id}" '
            f'data-ad-slot="{unit_id}">'
            f'<!-- ad: {slot.slot_key} (adsense) --></div>'
        )

    def render_head_script(self) -> str:
        if not self._client_id:
            return ""
        return (
            f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={self._client_id}" '
            f'crossorigin="anonymous"></script>'
        )


# --------------------------------------------------------------------------- #
# Provider registry
# --------------------------------------------------------------------------- #
def get_provider() -> AdProvider:
    """Instantiate the ad provider based on NEWSFORGE_AD_PROVIDER env var.

    Returns a configured provider instance or PlaceholderProvider when
    'none'/'placeholder' or when the configured provider is not properly set up."""
    from newsforge.config import AdConfig

    cfg = AdConfig()
    provider_name = cfg.provider.strip().lower()

    if provider_name == "adsense":
        provider = AdSenseProvider(
            client_id=cfg.adsense_client_id,
            slot_ids=cfg.adsense_slot_ids,
        )
        if provider.is_configured():
            return provider
        return PlaceholderProvider()

    return PlaceholderProvider()


def render_ad_slot_html(slot: AdSlot, *, provider: AdProvider | None = None) -> str:
    """Render the HTML placeholder for one ad slot.

    When a provider is given, delegates to ``provider.render_slot()``.
    Otherwise uses the default placeholder.  No external resources are
    loaded when no provider is configured."""
    if provider is not None:
        return provider.render_slot(slot)
    return (
        f'<div class="ad-slot" data-slot="{slot.slot_key}" '
        f'data-placement="{slot.placement}" data-fill="{slot.fill_type}">'
        f'<!-- ad: {slot.slot_key} --></div>'
    )


def insert_ad_slots(
    sections: list[dict],
    *,
    active_slots: list[dict] | None = None,
    provider: AdProvider | None = None,
) -> list[dict]:
    """Insert ad-slot markers into the body sections list.

    ``active_slots`` is a list of dicts with at least ``slot_key`` and
    ``placement`` keys (as returned by the ``ad_slots`` table).  When *None*,
    the five default positions are emitted with synthetic slot keys.

    ``provider`` is used to render each slot's HTML.  When *None*, the
    default placeholder is used.

    The returned list is a new list; the input is never mutated.

    Insertion logic:
    * HEADER   -> before the first section
    * AFTER_INTRO -> after the first ``intro`` section (or first section)
    * MID_ARTICLE -> after the midpoint section
    * BEFORE_RELATED -> before the last ``footer`` section (or at the end)
    * FOOTER   -> after the last section
    """
    if not sections:
        return sections

    slots = active_slots or [
        {"slot_key": f"ad_{p.lower()}", "placement": p, "fill_type": "DIRECT", "active": True}
        for p in DEFAULT_POSITIONS
    ]
    active = [s for s in slots if s.get("active", True)]
    if not active:
        return list(sections)

    placement_map: dict[str, dict] = {s["placement"]: s for s in active}

    result: list[dict] = []
    n = len(sections)
    mid_idx = n // 2

    inserted: set[str] = set()

    for i, sec in enumerate(sections):
        if AdPosition.HEADER in placement_map and AdPosition.HEADER not in inserted:
            result.append(_ad_marker(placement_map[AdPosition.HEADER], provider=provider))
            inserted.add(AdPosition.HEADER)

        result.append(sec)

        if (AdPosition.AFTER_INTRO in placement_map
                and AdPosition.AFTER_INTRO not in inserted
                and sec.get("type") == "intro"):
            result.append(_ad_marker(placement_map[AdPosition.AFTER_INTRO], provider=provider))
            inserted.add(AdPosition.AFTER_INTRO)

        if (AdPosition.MID_ARTICLE in placement_map
                and AdPosition.MID_ARTICLE not in inserted
                and i == mid_idx):
            result.append(_ad_marker(placement_map[AdPosition.MID_ARTICLE], provider=provider))
            inserted.add(AdPosition.MID_ARTICLE)

    if AdPosition.BEFORE_RELATED in placement_map and AdPosition.BEFORE_RELATED not in inserted:
        last_footer = len(result)
        for j in range(len(result) - 1, -1, -1):
            if result[j].get("type") == "footer":
                last_footer = j
                break
        result.insert(last_footer, _ad_marker(placement_map[AdPosition.BEFORE_RELATED], provider=provider))
        inserted.add(AdPosition.BEFORE_RELATED)

    if AdPosition.FOOTER in placement_map and AdPosition.FOOTER not in inserted:
        result.append(_ad_marker(placement_map[AdPosition.FOOTER], provider=provider))
        inserted.add(AdPosition.FOOTER)

    return result


def _ad_marker(slot: dict, *, provider: AdProvider | None = None) -> dict:
    """Build a section dict that represents an ad-slot marker."""
    ad_slot = AdSlot(
        slot_key=slot.get("slot_key", "ad_unknown"),
        placement=slot.get("placement", "UNKNOWN"),
        fill_type=slot.get("fill_type", "DIRECT"),
    )
    return {
        "type": "ad_slot",
        "slot_key": ad_slot.slot_key,
        "placement": ad_slot.placement,
        "fill_type": ad_slot.fill_type,
        "text": render_ad_slot_html(ad_slot, provider=provider),
    }


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #
def register_default_slots(session) -> list[dict]:
    """Idempotently register the five default ad-slot positions.

    Returns the list of slot dicts (for direct use by ``insert_ad_slots``)."""
    defaults = [
        {"slot_key": "header", "placement": AdPosition.HEADER, "fill_type": "DIRECT", "active": True, "cpm_cpc": 0.0, "min_bid": 0.0},
        {"slot_key": "after_intro", "placement": AdPosition.AFTER_INTRO, "fill_type": "DIRECT", "active": True, "cpm_cpc": 0.0, "min_bid": 0.0},
        {"slot_key": "mid_article", "placement": AdPosition.MID_ARTICLE, "fill_type": "DIRECT", "active": True, "cpm_cpc": 0.0, "min_bid": 0.0},
        {"slot_key": "before_related", "placement": AdPosition.BEFORE_RELATED, "fill_type": "DIRECT", "active": True, "cpm_cpc": 0.0, "min_bid": 0.0},
        {"slot_key": "footer", "placement": AdPosition.FOOTER, "fill_type": "DIRECT", "active": True, "cpm_cpc": 0.0, "min_bid": 0.0},
    ]
    registered = []
    for d in defaults:
        existing = session.query(ad_slots).filter_by(slot_key=d["slot_key"]).first()
        if existing is None:
            row = ad_slots(**d)
            session.add(row)
            registered.append(d)
        else:
            registered.append({
                "slot_key": existing.slot_key,
                "placement": existing.placement,
                "fill_type": existing.fill_type,
                "active": existing.active,
            })
    session.commit()
    return registered


def load_active_slots(session) -> list[dict]:
    """Load all active ad slots from the database."""
    rows = session.query(ad_slots).filter_by(active=True).order_by(ad_slots.slot_key).all()
    return [
        {
            "slot_key": r.slot_key,
            "placement": r.placement,
            "fill_type": r.fill_type,
            "active": r.active,
        }
        for r in rows
    ]
