"""Ad-slot architecture: position-based ad insertion for published articles.

This module provides the infrastructure for inserting advertisement slots into
article pages at configurable positions.  No external ad provider is integrated
yet — the architecture is ready for AdSense, Mediavine, or any other provider
that supplies a fill script or HTML snippet.

Usage::

    from newsforge.ads import insert_ad_slots, register_default_slots

    # At startup (optional): populate the ad_slots table with defaults.
    register_default_slots(session)

    # At render time: insert slot markers into the article sections list.
    sections_with_ads = insert_ad_slots(sections)
"""
from __future__ import annotations

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


def render_ad_slot_html(slot: AdSlot) -> str:
    """Render the HTML placeholder for one ad slot.

    The output is a single ``<div>`` with semantic classes and data attributes
    that an external ad script (AdSense, etc.) can target.  No external
    resources are loaded when no provider is configured."""
    return (
        f'<div class="ad-slot" data-slot="{slot.slot_key}" '
        f'data-placement="{slot.placement}" data-fill="{slot.fill_type}">'
        f'<!-- ad: {slot.slot_key} --></div>'
    )


def insert_ad_slots(sections: list[dict], *, active_slots: list[dict] | None = None) -> list[dict]:
    """Insert ad-slot markers into the body sections list.

    ``active_slots`` is a list of dicts with at least ``slot_key`` and
    ``placement`` keys (as returned by the ``ad_slots`` table).  When *None*,
    the five default positions are emitted with synthetic slot keys.

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

    # Build a placement->slot mapping.
    placement_map: dict[str, dict] = {s["placement"]: s for s in active}

    result: list[dict] = []
    n = len(sections)
    mid_idx = n // 2

    # Track which ad positions have been inserted.
    inserted: set[str] = set()

    for i, sec in enumerate(sections):
        # HEADER: before everything.
        if AdPosition.HEADER in placement_map and AdPosition.HEADER not in inserted:
            result.append(_ad_marker(placement_map[AdPosition.HEADER]))
            inserted.add(AdPosition.HEADER)

        result.append(sec)

        # AFTER_INTRO: after the first intro section.
        if (AdPosition.AFTER_INTRO in placement_map
                and AdPosition.AFTER_INTRO not in inserted
                and sec.get("type") == "intro"):
            result.append(_ad_marker(placement_map[AdPosition.AFTER_INTRO]))
            inserted.add(AdPosition.AFTER_INTRO)

        # MID_ARTICLE: after the midpoint section.
        if (AdPosition.MID_ARTICLE in placement_map
                and AdPosition.MID_ARTICLE not in inserted
                and i == mid_idx):
            result.append(_ad_marker(placement_map[AdPosition.MID_ARTICLE]))
            inserted.add(AdPosition.MID_ARTICLE)

        # BEFORE_RELATED / FOOTER: handled after the loop.

    # BEFORE_RELATED: before the last footer (or at the end).
    if AdPosition.BEFORE_RELATED in placement_map and AdPosition.BEFORE_RELATED not in inserted:
        # Find last footer index.
        last_footer = len(result)
        for j in range(len(result) - 1, -1, -1):
            if result[j].get("type") == "footer":
                last_footer = j
                break
        result.insert(last_footer, _ad_marker(placement_map[AdPosition.BEFORE_RELATED]))
        inserted.add(AdPosition.BEFORE_RELATED)

    # FOOTER: after everything.
    if AdPosition.FOOTER in placement_map and AdPosition.FOOTER not in inserted:
        result.append(_ad_marker(placement_map[AdPosition.FOOTER]))
        inserted.add(AdPosition.FOOTER)

    return result


def _ad_marker(slot: dict) -> dict:
    """Build a section dict that represents an ad-slot marker."""
    return {
        "type": "ad_slot",
        "slot_key": slot.get("slot_key", "ad_unknown"),
        "placement": slot.get("placement", "UNKNOWN"),
        "fill_type": slot.get("fill_type", "DIRECT"),
        "text": render_ad_slot_html(AdSlot(
            slot_key=slot.get("slot_key", "ad_unknown"),
            placement=slot.get("placement", "UNKNOWN"),
            fill_type=slot.get("fill_type", "DIRECT"),
        )),
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
