"""Risk classification (section 8).

Deterministic GREEN / YELLOW / ORANGE / RED severity. Risk is NOT driven by topic alone -- it
combines explicit keyword signals in the claim text with sensitivity floors for certain
information types (health, financial, etc.). The first matching severity wins, so a claim
containing an allegation is always at least as severe as any softer signal.

This is intentionally conservative: erring toward higher risk only ever *withholds* publishing,
never publishes something it should not have (section 11). A RED classification can never be
overridden by a high trust score -- the Decision Engine enforces that hard rule separately.
"""
from __future__ import annotations

import re


# Severity levels ordered from least to most severe.
RISK_LEVELS: tuple[str, ...] = ("GREEN", "YELLOW", "ORANGE", "RED")

# How much each severity deducts from a trust score (0-100). Higher risk -> bigger deduction.
RISK_DEDUCTION: dict[str, int] = {
    "GREEN": 0,
    "YELLOW": 3,
    "ORANGE": 10,
    "RED": 25,
}

# Information types that force at least this risk floor, even without keywords.
TYPE_FLOOR: dict[str, str] = {
    "health": "YELLOW", "medical": "YELLOW", "financial": "YELLOW",
}


def _variants(keyword: str) -> set[str]:
    """Exact word forms a keyword should match (base + regular inflections + tricky forms).

    English drops the final -e before -ing ("embezzle" -> "embezzling"), so suffix-concatenation
    cannot recover it. Known irregular/plural forms are listed explicitly; simple regular
    inflections (+s/+ed/+ing) cover nouns and verbs. Matching is exact (whole-word), which keeps
    false positives low while remaining deterministic (section 15).
    """
    base = {keyword}
    tricky = {
        "embezzle": {"embezzles", "embezzled", "embezzling"},
        "accuse": {"accuses", "accused", "accusing"},
        "increase": {"increases", "increased", "increasing"},
        "decrease": {"decreases", "decreased", "decreasing"},
    }
    for form in tricky.get(keyword, ()):
        base.add(form)
    for suffix in ("s", "ed", "ing"):
        candidate = keyword + suffix
        if candidate != keyword:
            base.add(candidate)
    return base


# (level, keywords) -- evaluated most-severe-first; first match wins.
RISK_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("RED", (
        "accuse", "allegation", "embezzle", "corruption", "crime", "criminal",
        "arrest", "detain", "suicide", "self-harm", "harmful", "dangerous", "weapon",
        "breach", "cyberattack", "hack", "attack", "emergency", "court case",
        "litigation", "defamation", "rumor", "hoax",
    )),
    ("ORANGE", (
        "politics", "election", "government", "official", "lawsuit", "investigation",
        "controversy", "sanction", "security incident", "national security",
    )),
    ("YELLOW", (
        "financial", "money", "price", "market", "health", "medical", "drug",
        "treatment", "salary", "loan", "subsidy", "grant",
    )),
]

# Precomputed exact-word forms per keyword.
_KEYWORD_VARIANTS: dict[str, set[str]] = {kw: _variants(kw) for rule in RISK_RULES for kw in rule[1]}

# Multi-word phrases that tokenization would otherwise split; matched as substrings. Mapped to
# their severity so precedence is preserved (section 15).
_PHRASES: dict[str, str] = {
    "court case": "RED", "self harm": "RED", "national security": "ORANGE",
    "security incident": "ORANGE", "self-harm": "RED",
}

_WORD_RE = re.compile(r"[a-z\u00e0-\u00ff]+")


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return {_w.lower() for _w in _WORD_RE.findall(text or "")}


def classify_risk(text: str | None, *, info_type: str = "news") -> str:
    """Return the risk level (GREEN/YELLOW/ORANGE/RED) for a piece of information.

    Rules are evaluated most-severe-first so the highest applicable severity wins. A
    ``TYPE_FLOOR`` from the information type can only *raise* the result, never lower it. Pure
    and deterministic (section 15).
    """
    tokens = _tokens(text)
    level = "GREEN"
    for rule_level, keywords in RISK_RULES:
        matched = any(tokens & _KEYWORD_VARIANTS[kw] for kw in keywords)
        if not matched and text:
            matched = any(_PHRASES[p] == rule_level and p.lower() in (text or "").lower()
                          for p in _PHRASES)
        if matched:
            level = rule_level
            break

    type_floor = TYPE_FLOOR.get(str(info_type).lower())
    if type_floor and _RANK(type_floor) > _RANK(level):
        level = type_floor
    return level


def risk_deduction(risk_level: str | None) -> int:
    """Trust-score penalty (0-100) applied for a given risk level."""
    if not risk_level:
        return 0
    return RISK_DEDUCTION.get(str(risk_level).upper(), 0)


def _RANK(level: str) -> int:
    try:
        return RISK_LEVELS.index(level.upper())
    except ValueError:
        return -1
