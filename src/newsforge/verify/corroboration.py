"""Corroboration & contradiction detection (section 3, 5).

Two hard rules drive this module:

1. **Independent corroboration counts DISTINCT sources**, not distinct articles. Two
   articles that merely repeat the same false source are NOT independent support -- they
   collapse to a single source. This is enforced relationally (``claim_evidence`` links claims
   to ``source_items``, which link to ``sources``) and here in pure helpers.

2. **Contradictions never publish silently.** We detect them deterministically via explicit
   action *axes* (approved vs rejected share one axis with opposite polarity), plus the
   authoritative fact-check path. The system must never invent a resolution -- it only flags
   ``CONTRADICTED`` and escalates to WAIT/REVIEW.
"""
from __future__ import annotations


def tokenize(text: str | None) -> set[str]:
    """Return the lowercase word tokens of ``text`` (whole words).

    Shared by the corroboration and risk engines so both classify the exact same words.
    Deterministic and pure (section 15).
    """
    if not text:
        return set()
    import re
    return {_w.lower() for _w in re.findall(r"[a-z\u00e0-\u00ff]+", text or "")}


# Action words mapped to a shared AXIS and POLARITY. Words on the same axis assert the same
# underlying fact; opposite polarity means two sources disagree (section 5). Both members of an
# opposition share ONE axis name so cross-source contradictions like "approved" vs "rejected"
# are detected deterministically, without an LLM.
ACTION_AXES: dict[str, tuple[str, int]] = {
    # approve / accept axis (+1)
    "approve": ("APPROVE_REJECT", 1), "approved": ("APPROVE_REJECT", 1), "accept": ("APPROVE_REJECT", 1),
    "pass": ("APPROVE_REJECT", 1), "passed": ("APPROVE_REJECT", 1), "win": ("APPROVE_REJECT", 1),
    "increase": ("APPROVE_REJECT", 1), "rise": ("APPROVE_REJECT", 1), "grow": ("APPROVE_REJECT", 1),
    "aprobó": ("APROBAR_RECHAZAR", 1), "aprobar": ("APROBAR_RECHAZAR", 1), "aceptó": ("APROBAR_RECHAZAR", 1),
    "pasó": ("APROBAR_RECHAZAR", 1), "aumentó": ("APROBAR_RECHAZAR", 1), "subió": ("APROBAR_RECHAZAR", 1),
    # reject / deny axis (-1)
    "reject": ("APPROVE_REJECT", -1), "rejected": ("APPROVE_REJECT", -1), "deny": ("APPROVE_REJECT", -1),
    "fail": ("APPROVE_REJECT", -1), "failed": ("APPROVE_REJECT", -1), "lose": ("APPROVE_REJECT", -1),
    "decrease": ("APPROVE_REJECT", -1), "fall": ("APPROVE_REJECT", -1), "shrink": ("APPROVE_REJECT", -1),
    "rechazar": ("APROBAR_RECHAZAR", -1), "rechazó": ("APROBAR_RECHAZAR", -1), "denegó": ("APROBAR_RECHAZAR", -1),
    "falló": ("APROBAR_RECHAZAR", -1), "bajó": ("APROBAR_RECHAZAR", -1), "disminuyó": ("APROBAR_RECHAZAR", -1),
}


def axis_of(text: str | None) -> tuple[str, int] | None:
    """Return the (axis, polarity) asserted by ``text``, or None if no action word is present."""
    for token in tokenize(text):
        entry = ACTION_AXES.get(token)
        if entry:
            return entry
    return None


def independent_sources_from(source_ids) -> int:
    """Count DISTINCT non-empty sources among a list of source identifiers.

    This is the core of section 3: two evidence rows pointing at the same source (or the same
    source_item) count as ONE independent source, not two. Deterministic and pure so it can
    be unit-tested without a database.
    """
    distinct = set()
    for sid in source_ids or []:
        if sid:
            distinct.add(str(sid))
    return len(distinct)


def detect_polarity_conflict(claims) -> list[tuple[int, int]]:
    """Find claim pairs that assert opposite polarity on the SAME action axis.

    ``claims`` is a sequence of dicts with at least a ``text`` key. Returns a list of
    ``(i, j)`` index pairs (i < j). Pure and deterministic -- used as a structural signal; the
    authoritative contradiction path is fact-checks (section 5).
    """
    conflicts: list[tuple[int, int]] = []
    axes_by_claim = [(axis_of(c.get("text")) if c else None) for c in claims]
    n = len(axes_by_claim)
    for i in range(n):
        axis_i = axes_by_claim[i]
        if not axis_i:
            continue
        for j in range(i + 1, n):
            axis_j = axes_by_claim[j]
            if axis_j and axis_i[0] == axis_j[0] and axis_i[1] != axis_j[1]:
                conflicts.append((i, j))
    return conflicts
