"""Story Engine — detect persistent stories from ingested signals (§4).

A *story* is a persistent entity (a ``STORY_ID``) that groups related news items
over time. When new information arrives we must be able to say: "this belongs to
the story already about X" rather than creating noise.

MVP approach (deterministic, no LLM): each item is reduced to an *event signature*
``(subject, product, concepts-or-kind, year)``. The resulting STORY_ID keeps items
that genuinely describe the *same event or subject* together while separating items
that merely share a broad ``(topic, year)`` bucket:

- ``subject`` is a closed vocabulary of known entities (organisations, products,
  people, institutions) matched longest-first; when none matches, a capitalized
  title phrase is used, or else a single capitalized title token. Boilerplate
  ("Latest news bulletin", "Tech Life", months, auxiliaries...) never becomes a
  subject.
- ``concepts`` is a separate closed event lexicon (biological weapons, existential
  risk, mathematics, ...) read from the TITLE only and independent from the claims
  layer's ``_EVENT_CONCEPT_PATTERNS``, so clustering never reuses the evidence
  predicate (:func:`newsforge.verify.claims.evidence_matches`).
- ``product`` disambiguates digit-bearing product names (Pixel 11 vs Pixel 11 Pro).
- ``kind`` (a coarse verb bucket) is used ONLY when no event concept fired, so two
  items whose only common words are generic ("report", "says") stay apart.

This fixes the contamination bug where two unrelated storage buckets collapsing
into one wide ``continue_technology_2026`` story let a single RED claim block every
other event. The old rule -- grouping by ``(topic, year)`` plus a cluster-wide
*dominant entity* phrase that picked feed boilerplate ("Continue", "Co") -- is gone.

All functions are pure so they can be unit-tested deterministically.
"""
from __future__ import annotations

import re

# Default topic taxonomy: slug -> significant keywords (single- or multi-word).
# Multi-word keywords are matched by checking whether ANY of their significant words
# appear in the text (§51 originality / §33 multilingual recall), so Spanish and
# English variants both work. Extend via config later without touching the algorithm.
DEFAULT_TOPIC_KEYWORDS: dict[str, list[str]] = {
    "package_tax": ["package", "paquete", "small package", "impuesto", "tax"],
    "eu_policy": ["eu", "ue", "european union", "unión europea", "brussels", "eurozone", "eu policy"],
    "health": ["health", "salud", "disease", "enfermedad", "vaccine", "vacuna", "hospital", "covid", "sanidad"],
    "economy": ["economy", "economía", "inflation", "inflación", "gdp", "interest rate", "bank", "banco", "market", "mercado"],
    "technology": ["ai", "artificial intelligence", "inteligencia artificial", "tech", "software", "chip", "semiconductor", "tecnología"],
    "climate": ["climate", "clima", "carbon", "emisiones", "renewable", "renovable", "energy transition", "energía"],
    "security": ["cybersecurity", "ciberseguridad", "cyber attack", "ataque cibernético", "breach", "brecha", "hack", "malware"],
}

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "by",
    "is", "are", "was", "were", "be", "it", "that", "this", "these", "those",
    "new", "say", "said", "says", "year", "years", "day", "days", "week",
}

_WORD_RE = re.compile(r"[A-Za-z]+")
_CAPWORD_RE = re.compile(r"\b([A-Z][A-Za-z-]+(?:\s+[A-Z][A-Za-z-]+)*)")


def slugify(text: str | None, max_len: int = 60) -> str:
    """Lowercase, underscore-ize and trim a slug component."""
    if not text:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:max_len]


def significant_tokens(text: str | None) -> list[str]:
    """Lowercase word tokens (len>=3) excluding stopwords."""
    if not text:
        return []
    return [t for t in _WORD_RE.findall(text or "") if len(t) >= 3 and t.lower() not in STOPWORDS]


def classify_topic(text: str | None, topics: dict[str, list[str]] | None = None) -> tuple[str, float]:
    """Return ``(topic_slug, score)`` for the best-matching topic.

    ``score`` is the number of keyword phrases whose significant words appear in the
    text. A phrase with zero hits does **not** win — when nothing matches we return
    ``("", 0.0)`` so callers can tell "no detectable topic" apart from a real match.
    Ties break by insertion order (deterministic).
    """
    topics = topics or DEFAULT_TOPIC_KEYWORDS
    tokens = set(significant_tokens(text))
    best_slug, best_score = "", 0
    for slug, keywords in topics.items():
        score = sum(1 for kw in keywords if any(w.lower() in tokens for w in significant_tokens(kw)))
        if score > best_score:
            best_score, best_slug = score, slug
    return best_slug, float(best_score)


def extract_year(value: str | None) -> int | None:
    """Extract a 4-digit year from an ISO date string or free text."""
    if not value:
        return None
    m = re.search(r"\b(19|20)\d{2}\b", value)
    return int(m.group(0)) if m else None


# --------------------------------------------------------------------------- #
# Event-signature clustering (deterministic; no LLM)
#
# A STORY_ID is built from ``[subject] [+ product] + (concepts | kind) + [year]``.
# The old ``(topic, year)`` grouping plus a cluster-wide "dominant entity" is gone:
# it picked feed boilerplate ("Continue", "Co") and fused every technology item
# into one bucket, so a single RED claim blocked the whole bucket.
# --------------------------------------------------------------------------- #

# Known entities: (case-insensitive match, canonical slug). Matched longest-first
# so "white house" wins over shorter overlaps. Only strong named things
# (organisations, products, people, institutions, governments) belong here;
# generic places or nouns fall back to title-phrase/token extraction and therefore
# never drag unrelated subjects into one over-broad bucket.
KNOWN_ENTITIES: list[tuple[str, str]] = [
    ("white house", "white_house"),
    ("central bank", "central_bank"),
    ("european union", "european_union"),
    ("unión europea", "european_union"),
    ("union europea", "european_union"),
    ("grand theft auto", "grand_theft_auto"),
    ("elon musk", "elon_musk"),
    ("john ternus", "john_ternus"),
    ("peter thiel", "peter_thiel"),
    ("silicon valley", "silicon_valley"),
    ("yuja wang", "yuja_wang"),
    ("lara croft", "lara_croft"),
    ("gene marks", "gene_marks"),
    ("new mexico", "new_mexico"),
    ("anthropic", "anthropic"),
    ("instagram", "instagram"),
    ("openai", "openai"),
    ("huawei", "huawei"),
    ("google", "google"),
    ("apple", "apple"),
    ("pixel", "pixel"),
    ("meta", "meta"),
    ("musk", "elon_musk"),
    ("gta", "grand_theft_auto"),
    ("ninja", "ninja"),
    ("flock", "flock"),
    ("marvel", "marvel"),
    ("eu", "european_union"),
    ("ue", "european_union"),
    ("uk", "uk"),
]
_KNOWN_ENTITIES = sorted(KNOWN_ENTITIES, key=lambda kv: -len(kv[0]))
_ENTITY_RE = [
    (re.compile(r"(?<![\w])" + re.escape(marker).lower() + r"(?![\w])"), canonical)
    for marker, canonical in _KNOWN_ENTITIES
]


def _known_entity(text: str) -> str | None:
    """First KNOWN_ENTITIES match in ``text`` (longest match wins)."""
    if not text:
        return None
    lowered = text.lower()
    for pattern, canonical in _ENTITY_RE:
        if pattern.search(lowered):
            return canonical
    return None


# Words that never become a story subject. Title-case makes every token look like a
# proper noun, so months, auxiliaries, wh-words and feed-section labels are excluded.
COMMON_WORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "by",
    "is", "are", "was", "were", "be", "been", "being", "it", "its", "that", "this",
    "these", "those", "new", "now", "say", "said", "says", "year", "years", "day",
    "days", "week", "today", "tomorrow", "yesterday",
    "do", "does", "did", "done", "doing", "can", "could", "should", "would",
    "will", "shall", "may", "might", "must", "have", "has", "had", "having",
    "how", "why", "what", "when", "where", "who", "whom", "whose", "which",
    "over", "under", "after", "before", "during", "while", "about", "into",
    "from", "you", "your", "us", "they", "their", "them", "we", "our", "i",
    "me", "my", "he", "she", "his", "her", "many", "much", "such", "at", "least",
    "up", "down", "off", "out", "overall", "more", "most", "every", "each", "both",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "former", "first", "second", "third", "fourth", "fifth", "last",
    "hundred", "thousand", "million", "billion", "trillion",
    "el", "la", "los", "las", "un", "una", "del", "al", "en", "por", "para",
    "que", "con", "sin", "de", "ser", "es", "son", "se", "su", "sus", "otro",
    "otra", "otros", "otras", "nuevo", "nueva", "nuevos", "nuevas",
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "latest", "news", "bulletin", "tech", "life", "morning", "evening",
    "midday", "noon", "update", "updates", "breaking", "developing", "live",
    "report", "analysis", "special", "exclusive", "big", "little", "world", "ai",
    "social", "behind", "major", "massive", "thousands", "hundreds", "ahead",
    "inside", "outside", "amid", "among", "without", "against", "facing", "tv",
    "rise", "star",
}

# Multi-word title phrases that are feed chrome (bulletin headers, boilerplate).
COMMON_PHRASES = {
    "latest news bulletin", "continue reading", "tech life", "tech now",
    "state handouts", "war", "25 years on",
}

_MONTHS = {"january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"}


def _title_phrases(title: str) -> list[str]:
    """Capitalized title runs in positional order, minus single-char tokens."""
    phrases = []
    for phrase in _CAPWORD_RE.findall(title or ""):
        words = [w for w in phrase.split() if len(re.sub(r"[^A-Za-z]", "", w)) >= 2]
        if words:
            phrases.append(" ".join(words))
    return phrases


def _subject_of(item: dict) -> str:
    """Deterministic subject: title entity > title phrase > description entity.

    The TITLE is the primary event signal; names that only appear in a description
    (e.g. "UE" inside a Spanish description) are used only when the title yields no
    subject at all, so a "Meta..." headline is never stolen by an "Instagram"
    mention buried in its body.
    """
    title = item.get("title") or ""
    known = _known_entity(title)
    if not known:
        known = _known_entity(" ".join(x for x in (
            title, item.get("description") or "", item.get("content_text") or "",
        ) if x))
    if known:
        return known

    for phrase in _title_phrases(title):
        if any(c in phrase for c in "0123456789"):
            continue  # digit phrases belong to the product slot, not the subject
        words = [w.lower() for w in phrase.split()]
        while words and words[0] in COMMON_WORDS:
            words = words[1:]
        if words:
            return slugify(" ".join(words))
    return ""


# Digit-bearing product names (Pixel 11 Pro, Tesla Model 3, GTA VI). Years/months
# and currency-prefixed figures are excluded so "September 11", "€13bn" and
# "2026" never become products.
_PRODUCT_RE = re.compile(r"\b([A-Z][A-Za-z]+)\s+([0-9]+)(?:\s+([A-Z][A-Za-z]+))?\b")


def _product_of(item: dict) -> str:
    """First digit-bearing product name in the title (Pixel 11 Pro -> pixel_11_pro).

    Calendar phrases ("September 11", "September 11th, 2026") and plain years are
    NOT products, so a "September 2026 report" never becomes a product story.
    """
    title = item.get("title") or ""
    for m in _PRODUCT_RE.finditer(title):
        head = m.group(1).lower()
        digits = m.group(2)
        if head in _MONTHS:
            continue
        if re.fullmatch(r"(19|20)\d{2}", digits) and not m.group(3):
            continue  # a bare year is a date, not a product name
        tail = m.group(3) or ""
        if tail and tail.lower() in _MONTHS:
            continue
        name = f"{m.group(1)} {digits} {tail}".strip()
        yield slugify(name)


def _title_concepts(title: str) -> list[str]:
    """Event concepts detected in the TITLE text (closed lexicon, deterministic).

    This lexicon lives in the stories layer ON PURPOSE: clustering must stay
    independent from the evidence predicate in :mod:`newsforge.verify.claims`
    so a signal that clusters two items can never, by construction, also corrupt
    the corroboration verdict (layered pipelines).
    """
    if not title:
        return []
    lowered = title.lower()
    found = [
        slug for slug, patterns in _EVENT_CONCEPT_PATTERNS.items()
        if any(p.search(lowered) for p in patterns)
    ]
    return sorted(found)


# Closed event-concept lexicon: concept -> regularlys that fire on the title text.
# Each concept is a distinct *event frame*; two items share a story key only when
# their frames coincide (subject + product + same concept set), so a bioweapons
# report and an existential-risk report about the same company never fuse.
_EVENT_CONCEPT_PATTERNS: dict[str, list[re.Pattern]] = {
    "biological_weapons": [
        re.compile(r"bioweapon(ry|s)?"), re.compile(r"bio[\s-]?weapon"),
        re.compile(r"biological weapons?"),
    ],
    "existential_risk": [
        re.compile(r"kill all humans?"), re.compile(r"human extinction"),
        re.compile(r"existential"), re.compile(r"perils?"), re.compile(r"wipe out"),
    ],
    "mathematics": [
        re.compile(r"maths"), re.compile(r"\bmathematics\b"), re.compile(r"math problem"),
        re.compile(r"olympiad"),
    ],
    "ai_safety": [
        re.compile(r"kill switch"), re.compile(r"dangerous ai"),
        re.compile(r"ai safety"), re.compile(r"dangerous artificial intelligence"),
    ],
    "child_sexual_abuse": [
        re.compile(r"child sexual"), re.compile(r"\bcsam\b"), re.compile(r"paedophil"),
        re.compile(r"pedophil"), re.compile(r"nude images?"),
    ],
    "scam": [
        re.compile(r"\bscams?"), re.compile(r"\bfraud"), re.compile(r"\bphishing"),
        re.compile(r"\bransoms?"),
    ],
    "copyright": [
        re.compile(r"copyright"), re.compile(r"intellectual property"),
        re.compile(r"\bplagiar"),
    ],
    "investment": [
        re.compile(r"\binvest(?:s|ing|ed|ment|ments|or|ors)?\b"),
        re.compile(r"data cent[er]e"), re.compile(r"\bfunding"),
    ],
    "product_review": [
        re.compile(r"\breview"), re.compile(r"hands-on"), re.compile(r"first play"),
        re.compile(r"\bverdict"), re.compile(r"\btested"),
    ],
    "law": [
        re.compile(r"\blaws?\b"), re.compile(r"legislation"), re.compile(r"\bbill\b"),
        re.compile(r"regulation"),
    ],
    "economy": [
        re.compile(r"\bgdp\b"), re.compile(r"inflation"), re.compile(r"interest rates?"),
        re.compile(r"\beconomy\b"), re.compile(r"\beconomic"), re.compile(r"\brecession"),
    ],
    "battery": [
        re.compile(r"\bbattery"),
    ],
    "tax": [
        re.compile(r"\btaxes?\b"), re.compile(r"\btaxed\b"),
    ],
    "package_tax": [
        re.compile(r"package tax(?:es)?\b"), re.compile(r"small package"),
        re.compile(r"impuesto.{0,12}paquet"), re.compile(r"\bpaquet\w*"),
    ],
}


# Verb buckets: a coarse *kind* is used ONLY when no event concept fired. Stem
# matching is one-way (token starts with any stem) and eager, so "launches" ->
# announce, "seizes" -> attack, "rejects" -> block.
KIND_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("announce", ("announc", "launch", "unveil", "introduc", "reveals", "reveal",
                  "open", "opens", "mark", "celebrat", "approv", "select", "pick",
                  "plans", "plan", "says", "say", "welcomes", "bids", "set", "reach",
                  "publishe")),
    ("warn", ("warn", "caution", "alert", "predict", "fears", "fear")),
    ("block", ("block", "reject", "ban", "bar", "refuse", "stop", "pull")),
    ("attack", ("attack", "strike", "hit", "kill", "seize", "destroy", "blast",
                "raid", "drone", "fire", "blitz", "destroys")),
    ("report", ("report", "detail", "study", "analys", "find", "shows", "show",
                "suggest", "accuse", "win", "wins", "lose", "loses", "claim",
                "claims", "designat", "brand", "branded", "fine", "fined", "trial",
                "overturn", "upheld", "convict", "sentenc", "arrest", "retire",
                "quit", "resign")),
)
def _kind_of(title: str) -> str:
    """Coarse verb bucket for a title; '' when nothing explicit matches."""
    if not title:
        return ""
    tokens = re.findall(r"[a-z]+", title.lower())
    lowercase = set(tokens)
    for bucket, stems in KIND_KEYWORDS:
        for stem in stems:
            if any(t.startswith(stem) for t in lowercase):
                return bucket
    return ""


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def event_signature(item: dict) -> dict:
    """Pure per-item event signature: ``{subject, product, concepts, kind, year}``."""
    title = item.get("title") or ""
    text = " ".join(x for x in (
        title, item.get("description") or "", item.get("content_text") or "",
    ) if x)
    concepts = _title_concepts(title)
    return {
        "subject": _subject_of(item),
        "product": next(_product_of(item), ""),
        "concepts": concepts,
        "kind": _kind_of(title) if not concepts else "",
        "year": extract_year(item.get("published_at")) or extract_year(text),
    }


def _story_key(sig: dict) -> str:
    """Build the deterministic STORY_ID from an event signature."""
    parts: list[str] = []

    subject = sig["subject"]
    concepts = sig.get("concepts") or []
    frame: str = ""
    if concepts:
        frame = "_".join(concepts)
    elif sig["kind"]:
        frame = sig.get("kind") or ""

    if subject:
        parts.append(subject)
    product = sig.get("product") or ""
    if product and product != subject:
        parts.append(product)
    if frame and frame != subject:
        parts.append(frame)

    year = sig.get("year")
    key = "_".join(parts)
    if not key:
        key = "uncategorized"
    if year is not None:
        key += f"_{year}"
    key = slugify(key).strip("_")
    return key or "story"


def classify_item(item: dict) -> dict[str, str | None]:
    """Classify a single item into its natural story key (pure; no DB access).

    Returns ``{"story_id", "topic_slug", "year"}``. ``story_id`` is the event-based
    key. ``topic_slug`` keeps the existing topic label for ``stories.topic``.
    """
    sig = event_signature(item)
    return {
        "story_id": _story_key(sig),
        "topic_slug": classify_topic(" ".join(x for x in (
            item.get("title") or "", item.get("description") or "",
            item.get("content_text") or "",
        ) if x))[0],
        "year": sig["year"],
    }


def detect_story_id(topic_slug: str | None, *, entity: str | None = None, year: int | None = None) -> str:
    """Build a stable STORY_ID (kept for back-compatibility with earlier callers).

    New code should use :func:`event_signature` + :func:`_story_key`; detection now
    keys on event signatures instead of ``(topic, entity, year)``.
    """
    parts = [p for p in (entity or "", topic_slug or "") if p]
    base = "_".join(parts)
    if not base and year:
        base = "uncategorized"
    if year:
        base += f"_{year}"
    return slugify(base).strip("_") or "story"


def cluster_items(items: list[dict]) -> dict[str, list[dict]]:
    """Group items into stories keyed by STORY_ID.

    Deterministic and order-independent: every item gets the same event signature
    whatever the input order, so :meth:`newsforge.stories.engine.StoryDetector.process`
    stays idempotent across repeated runs.

    Signature-keyed clustering replaces the old ``(topic, year)`` buckets, which
    fused every technology item into one ``continue_technology_2026`` story and let
    a single RED claim contaminate unrelated events.
    """
    if not items:
        return {}
    clusters: dict[str, list[dict]] = {}
    for item in items:
        sig = event_signature(item)
        clusters.setdefault(_story_key(sig), []).append(item)
    return clusters