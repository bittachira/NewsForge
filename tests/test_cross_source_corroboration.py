"""Regression tests: real cross-source corroboration must unlock ACCEPT verdicts.

Root cause this suite guards against (RUN 6a4c1d57): the default claim-spec
builder attached ONLY the claim's own source_item as evidence, so every claim
looked "single-source" to the Trust Engine even when two genuinely independent
sources were clustered into the SAME story. Corroboration never exceeded 1 and
trust could never reach the publish bar on a TIER_3 story with any risk/freshness
deduction.

The tests below drive the REAL pipeline (`run_pipeline` + default builder) so the
fix is proven end-to-end: detection -> clustering -> claims/evidence ->
corroboration -> trust -> decision.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from newsforge import db
from newsforge.db import decisions, get_session, publications
from newsforge.pipeline.orchestrator import (
    _default_claim_spec_builder,
    run_pipeline,
)

# One shared reference clock; every seeded item is ~8h old => fresh (100 pts).
_T0 = "2026-09-12T08:00:00+00:00"
_PUB = "2026-09-12T00:00:00+00:00"

_db_seq = 0


@pytest.fixture(autouse=True)
def isolated_db():
    global _db_seq
    db_dir = Path.cwd() / ".pytest_tmp"
    db_dir.mkdir(exist_ok=True)
    _db_seq += 1
    path = db_dir / f"xcorr_{_db_seq}.db"
    try:
        if path.exists():
            path.unlink()
    except OSError:
        _db_seq += 1
        path = db_dir / f"xcorr_{_db_seq}.db"
    with db.use_isolated_database_ctx(path):
        yield


@pytest.fixture(autouse=True)
def clean_registry():
    from newsforge.publish.destinations import (
        register_builtin_destinations,
        reset_registry,
    )
    reset_registry()
    register_builtin_destinations()
    yield
    reset_registry()


def _seed_source(session, *, id: str, tier: str = "TIER_3"):
    src = db.sources()
    src.id = id
    src.source_id = id
    src.name = id
    src.type = "RSS"
    src.country = "GB"
    src.language = "en"
    src.tier = tier
    src.trust_score = 60 if tier == "TIER_3" else 35
    src.status = "active"
    session.add(src)
    session.commit()
    return id


def _seed_item(session, *, source_id: str, title: str, description: str | None = None) -> str:
    item = db.source_items()
    item.source_id = source_id
    item.title = title
    item.description = description
    item.content_html = None
    item.content_text = description or title
    item.published_at = _PUB
    item.dedupe_hash = f"hash-{source_id}-{title}"
    session.add(item)
    session.commit()
    return str(item.id)


# Both headlines match the economy topic ("interest rate"/"market" keywords),
# share the entity "Central Bank" and carry the low-severity word "market"
# (YELLOW => -3 trust deduction). That makes a SINGLE-source story fall just
# below the 60 trust bar while a genuinely corroborated one passes.
_HEADLINES = [
    "Central Bank preserves interest rates after volatile market week",
    "Central Bank keeps interest rates amid volatile markets",
]


def _run(signal_ids):
    # Pin a single destination so publication-row counts are unambiguous.
    return run_pipeline(signal_ids=signal_ids, reference_time=_T0,
                        destinations=["recording"])


# --------------------------------------------------------------------------- #
# Bug reproduction: two articles from the SAME source must NOT count as 1... no,
# as corroboration; they must still collapse to ONE independent source.
# --------------------------------------------------------------------------- #
def test_same_source_items_do_not_create_fake_corroboration():
    with get_session() as s:
        _seed_source(s, id="src-euronews")
        ids = [_seed_item(s, source_id="src-euronews", title=t) for t in _HEADLINES]

    result = _run(ids)

    assert result["stories_detected"] == 1, "both items must cluster into one story"
    outcome = result["outcomes"][0]
    decision = outcome.decision
    te = decision["trust_evaluations"][0]
    assert te["independent_corroboration"] == 1, "same outlet == one independent source"
    assert te["total_evidence"] == 2, "both linked items are counted as evidence"
    assert decision["trust_score"] < 60, f"single-source trust must stay below bar: {decision['trust_score']}"
    assert outcome.final_status == "WAIT", (
        f"single-source story must not publish: {outcome.final_status}"
    )


# --------------------------------------------------------------------------- #
# The fix: two genuinely independent sources on the same story must be counted
# as 2 independent sources and cross the trust bar legitimately.
# --------------------------------------------------------------------------- #
def test_independent_sources_unlock_publish():
    with get_session() as s:
        _seed_source(s, id="src-alpha")
        _seed_source(s, id="src-beta")
        item_a = _seed_item(s, source_id="src-alpha", title=_HEADLINES[0])
        item_b = _seed_item(s, source_id="src-beta", title=_HEADLINES[1])

    result = _run([item_a, item_b])

    assert result["stories_detected"] == 1, "both sources must cluster into one story"
    outcome = result["outcomes"][0]
    decision = outcome.decision
    assert outcome.final_status == "PUBLISHED", (
        f"corroborated story should publish, got {outcome.final_status}: {outcome.error}"
    )
    assert decision["decision"] == "PUBLISH"
    assert decision["trust_score"] >= 60, f"trust too low: {decision['trust_score']}"
    for te in decision["trust_evaluations"]:
        assert te["independent_corroboration"] == 2
        assert te["total_evidence"] == 2

    with get_session() as s:
        assert s.query(publications).count() == 1


# --------------------------------------------------------------------------- #
# Builder unit regression: own item first, sibling items attached as evidence.
# --------------------------------------------------------------------------- #
def test_default_builder_attaches_story_evidence():
    with get_session() as s:
        _seed_source(s, id="src-a")
        _seed_source(s, id="src-b")
        item_a = _seed_item(s, source_id="src-a", title=_HEADLINES[0])
        item_b = _seed_item(s, source_id="src-b", title=_HEADLINES[1])
        st = db.stories()
        st.story_id = "central_bank_economy_2026"
        st.slug = "central_bank_economy_2026"
        st.title = _HEADLINES[0]
        session = s
        session.add(st)
        session.flush()
        for iid in (item_a, item_b):
            sig = db.story_signals()
            sig.story_id = st.story_id
            sig.item_id = iid
            session.add(sig)
        session.commit()

    with get_session() as s:
        specs = _default_claim_spec_builder(s, "handle", "central_bank_economy_2026")

    assert len(specs) == 2
    for spec in specs:
        # Own item is first in the evidence list, both items are attached.
        assert spec["source_item_ids"][0] in (item_a, item_b)
        assert set(spec["source_item_ids"]) == {item_a, item_b}
        assert spec["tiers"] == ["TIER_3"]


# --------------------------------------------------------------------------- #
# Idempotency: the corroborated run is deterministic and creates no duplicates.
# --------------------------------------------------------------------------- #
def test_corroborated_pipeline_is_idempotent():
    with get_session() as s:
        _seed_source(s, id="src-a")
        _seed_source(s, id="src-b")
        item_a = _seed_item(s, source_id="src-a", title=_HEADLINES[0])
        item_b = _seed_item(s, source_id="src-b", title=_HEADLINES[1])

    run1 = _run([item_a, item_b])
    run2 = _run([item_a, item_b])

    assert run1["outcomes"][0].final_status == "PUBLISHED"
    assert run2["outcomes"][0].final_status == "PUBLISHED"
    handle = run1["outcomes"][0].story_handle
    assert handle == run2["outcomes"][0].story_handle

    with get_session() as s:
        assert s.query(publications).filter_by(story_id=run1["outcomes"][0].business_key).count() == 1
        assert s.query(decisions).filter_by(target_id=run1["outcomes"][0].business_key).count() == 1
        # Story handle (UUID pk) is not a decision key column; identity is by business key.
        assert s.query(decisions).count() == 1


# --------------------------------------------------------------------------- #
# Semantic integrity: a cross-source item corroborates ONLY the claims it really
# supports.
#
# The headlines/description pairs below are VERBATIM from the production feeds
# (bbc_tech / guardian_tech on 2026-09-10-11). Verbatim real text is the guard
# against over-matching heuristics: it contains real boilerplate ("Continue
# reading", newsletter signup shims, HTML markup) and real shared vocabulary that
# any naive token matcher would latch onto.
# --------------------------------------------------------------------------- #
_BBC_BIOWEAPONS = "Anthropic blocks possible attempt to use AI to make biological weapons"
_BBC_BIOWEAPONS_DESC = (
    "The revelations in Anthropic's threat intelligence report come after a former "
    "top researcher at the company warned of the risks of AI to humanity."
)
_GUARDIAN_BIOWEAPONS = "Anthropic details bad actors' efforts to misuse its AI for bioweapons"
_GUARDIAN_BIOWEAPONS_DESC = (
    "<p>Report comes two days after former employee quit claiming company\u2019s models "
    "could cause human extinction by 2030</p><p>Criminals, state-sponsored groups, spyware "
    "vendors, scientists and propagandists have attempted to use Anthropic\u2019s powerful "
    "artificial intelligence models to design missiles and bombs, create deadly pathogens "
    "and surveil dissidents, according to a<a href='https://www.anthropic.com/threat"
    "-intelligence-report-september-2026#biological-misuse-sep-26'> threat intelligence "
    "report</a> the company published on Thursday.</p><p>\u201cThe cases we share here "
    "aren\u2019t typical misuse, but rather examples of the most notable and novel threat "
    "activity we\u2019ve identified to date,\u201d Anthropic wrote in its 154-page report. "
    "\u201cWe\u2019re publishing this work because we believe we have a responsibility to "
    "disclose malicious misuse of our services.\u201d</p> <a href='https://www.theguardian"
    ".com/technology/2026/sep/10/anthropic-report-details-ai-misuse'>Continue reading...</a>"
)

# More real items: a Google investment story, two phone reviews, an AI-comic
# story, an Instagram-misinformation story and a second Anthropic story about a
# DIFFERENT event (researchers' existential-risk warnings, which share the
# organism, the entity and the "threat report" context with the bioweapons one).
_REAL_ITEMS = [
    ("comic", "Does this AI comic make you laugh?",
     "Comedian Garrett Millerick has created an AI avatar based on his own material. Is it any good?"),
    ("bbc-bioweapons", _BBC_BIOWEAPONS, _BBC_BIOWEAPONS_DESC),
    ("google-finland", "Google picks Finland for its largest single investment in Europe",
     "The US tech giant says the €13bn data centre project will create tens of thousands of jobs."),
    ("researcher-10", "Anthropic researcher believes more than 10% chance AI 'could kill all humans'",
     "It is the latest in a series of increasing warnings about the safety threat posed by artificial intelligence."),
    ("scammers", "Scammers demand ransoms from Instagram users over fake copyright claims",
     "Users say they are losing money or risk having their accounts suspended because Meta struggles to identify scammers."),
    ("guardian-bioweapons", _GUARDIAN_BIOWEAPONS, _GUARDIAN_BIOWEAPONS_DESC),
    ("more-researchers",
     "More Anthropic researchers warn of AI's perils but Musk dismisses 'psyop'",
     ("Insiders at the firm fear tech's advancement could cause human extinction, "
      "while others are calling their declarations of concern a 'setup'. A day after a "
      "former researcher at Anthropic made an apocalyptic declaration about artificial "
      "intelligence, more researchers and staff members at the AI startup publicly "
      "agreed with him and posted their own dire warnings. In response, Elon Musk and "
      "other conservative figures on X called the chorus of concerns a 'setup' and a "
      "'psyop'.")),
    ("pixel-pro", "Pixel 11 Pro review: Google's best pocket camera goes customisable",
     ("Solid battery life, snappy performance and quality software make for a great "
      "smaller phone with a killer camera. Google's latest Pro phone is out to prove "
      "it has the best camera on a smartphone while embracing customisation, allowing "
      "you to change the look and feel of your photos far beyond simple filters even "
      "if that means making them technically worse. The Pixel 11 Pro packs the best of "
      "Google's hardware and software into a still-pocketable and easy to handle frame, "
      "instantly making it a contender for best smaller phone of the year. Screen: 6.3in "
      "120Hz QHD+ OLED (495ppi). Processor: Google Tensor G6. RAM: 12 or 16GB. Storage: "
      "256, 512GB or 1TB. Operating system: Android 17. Camera: 50MP+ 48MP UW + 48MP 5x "
      "tele; 42MP selfie. Connectivity: 5G, eSIM, wifi 7, UWB, NFC, Bluetooth 6, Thread "
      "and GNSS. Water resistance: IP68 (1.5m for 30 minutes). Dimensions: 152.7 x 71.9 "
      "x 8.4mm. Weight: 204g")),
    ("instagram-boss",
     "Instagram boss says users will be 'overwhelmed' with brand content in algorithm-free world",
     ("Adam Mosseri says platform's algorithm 'feels like a black box' but company "
      "trying to give users more control after Labor announces opt-out plan.")),
    # Real member of continue_technology_2026 (the SECOND Pixel review). Both phone
    # reviews share the "Google" entity; they must NOT corroborate each other.
    ("pixel-11",
     "Pixel 11 review: Google sets the bar for standard flagship phones",
     ("Longer battery life, faster chip, actually useful AI tools and better cameras "
      "keep quality Android ahead of competition. Google's Pixel 11 continues to set "
      "the standard for what you should expect from the base model of a flagship phone "
      "with class-leading cameras, long software support and almost all the bells and "
      "whistles of its most expensive phones. The regular Pixel 11 costs \u00a3879 "
      "(\u20ac999/$899/A$1,499) making it \u00a380 or equivalent more expensive than last "
      "year's model as the cost of RAMageddon continues to bite. It's not cheap by any "
      "stretch of the imagination, but it comes with 256GB of storage and is \u00a3200 "
      "less than the Pixel 11 Pro, matching rival Samsung's Galaxy S26. Screen: 6.3in "
      "120Hz FHD+ OLED (422ppi). Processor: Google Tensor G6. RAM: 12GB. Storage: 256 or "
      "512GB. Operating system: Android 17. Camera: 48MP+ UW 5x tele; selfie 13MP UW. "
      "Connectivity: 5G, eSIM, wifi 6E, UWB, NFC, Bluetooth 6 and GNSS. Water "
      "resistance: IP68 (1.5m for 30 minutes). Dimensions: 152.8 x 72.0 x 8.6mm. "
      "Weight: 197g")),
]

# The ONLY genuine same-event pair among the real items above.
_SAME_EVENT_PAIRS = {
    ("bbc-bioweapons", "guardian-bioweapons"),
    ("guardian-bioweapons", "bbc-bioweapons"),
}


def _evitem(title: str, description: str) -> dict:
    return {
        "subject_title": title,
        "subject_description": description,
        "candidate_title": title,
        "candidate_description": description,
    }


def test_evidence_matches_pure_predicate():
    from newsforge.verify.claims import evidence_matches

    assert evidence_matches(
        subject_title=_BBC_BIOWEAPONS,
        subject_description=_BBC_BIOWEAPONS_DESC,
        candidate_title=_GUARDIAN_BIOWEAPONS,
        candidate_description=_GUARDIAN_BIOWEAPONS_DESC,
    )
    assert not evidence_matches(
        subject_title="UK government rejects kill switch idea for dangerous AI",
        subject_description=None,
        candidate_title="Google picks Finland for Europe's largest AI data centre investment",
        candidate_description=None,
    )


def test_real_feed_cross_source_matrix_has_exactly_one_true_pair():
    """Real BBC/Guardian items: each ordered item pair matches iff it is the SAME event.

    Everything else -- including the second Anthropic story and the two phone
    reviews (both share the entity with other items) -- must stay no-match.
    """
    from newsforge.verify.claims import evidence_matches

    by_label = {label: (title, desc) for label, title, desc in _REAL_ITEMS}
    false_pairs: list[str] = []
    for left, (lt, ld) in by_label.items():
        for right, (rt, rd) in by_label.items():
            if left == right:
                continue
            actually = evidence_matches(
                subject_title=lt, subject_description=ld,
                candidate_title=rt, candidate_description=rd,
            )
            expected = (left, right) in _SAME_EVENT_PAIRS
            if actually != expected:
                false_pairs.append(f"{left} x {right}: got {actually}, want {expected}")
    assert not false_pairs, "\n".join(false_pairs)


def test_ai_comic_does_not_corroborate_microsoft_laptop_review():
    """Fixture C: an AI-comic story and a laptop review never corroborate.

    The BBC AI-comic item is real feed text; no Microsoft laptop review sits in the
    current bbc_tech/guardian_tech window, so the reviewer-side item uses the same
    product-review pattern as the real Pixel items.
    """
    from newsforge.verify.claims import evidence_matches

    assert not evidence_matches(
        subject_title="Does this AI comic make you laugh?",
        subject_description="Comedian Garrett Millerick has created an AI avatar based on his own material. Is it any good?",
        candidate_title="Microsoft Surface Laptop 8 review: a quality PC whose trackpad taps you back",
        candidate_description="The latest Surface keeps a superb build and long battery life, though the price is hard to swallow.",
    )


def test_bioweapons_story_does_not_merge_with_generic_ai_risk_story():
    """Same entity, same safety-report context -- but a DIFFERENT event (#6).

    'Anthropic ... biological weapons safeguards' and 'Anthropic ... AI risks' share
    the name and the topic; they must still NOT corroborate each other, because
    they describe different events and share no event-specific signal.
    """
    from newsforge.verify.claims import evidence_matches

    assert not evidence_matches(
        subject_title="Anthropic releases biological weapons safeguards",
        subject_description="The company installed new safeguards on models used for designing molecules.",
        candidate_title="Anthropic researchers warn AI could wipe out humanity",
        candidate_description="A group of researchers published a warning about the dangers of advanced models.",
    )


def test_boilerplate_never_contributes_signals():
    """Feed boilerplate must be stripped before matching (fixture E).

    Real BBC/Guardian descriptions end with 'Continue reading...' or newsletter
    shims; those tokens must never count as a corroborating signal, and two items
    whose only overlap is boilerplate must not match.
    """
    from newsforge.verify.claims import _clean_text, _significant_set, evidence_matches

    boilerplate = (
        "<p>Continue reading the main story.</p> "
        "<a href='https://example.com/x'>Sign up to our newsletter</a>"
        "Available for everyone, funded by readers. This article is more than a year old."
        "Read more."
    )
    assert "continue" not in _significant_set(boilerplate)
    assert "sign" not in _significant_set(boilerplate)
    assert "newsletter" not in _significant_set(boilerplate)
    assert _clean_text("<p>One clear head.</p>") == "One clear head."

    assert not evidence_matches(
        subject_title="Google raises the price of cloud services",
        subject_description=boilerplate,
        candidate_title="Dieterich Jones boosts its dividend payout",
        candidate_description=boilerplate,
    )


def test_cross_source_same_event_persists_two_evidence_rows():
    with get_session() as s:
        _seed_source(s, id="bbc-tech", tier="TIER_2")
        _seed_source(s, id="guardian-tech", tier="TIER_2")
        item_bbc = _seed_item(
            s, source_id="bbc-tech", title=_BBC_BIOWEAPONS, description=_BBC_BIOWEAPONS_DESC,
        )
        item_gdn = _seed_item(
            s, source_id="guardian-tech", title=_GUARDIAN_BIOWEAPONS,
            description=_GUARDIAN_BIOWEAPONS_DESC,
        )

    result = _run([item_bbc, item_gdn])

    assert result["stories_detected"] == 1, "both sources must cluster into one story"
    outcome = result["outcomes"][0]
    bk = outcome.business_key

    with get_session() as s:
        claim_rows = s.query(db.claims).filter_by(story_id=bk).all()
        assert len(claim_rows) == 2, "one claim per linked item"
        for claim in claim_rows:
            ev_rows = s.query(db.claim_evidence).filter_by(claim_id=str(claim.id)).all()
            assert len(ev_rows) == 2, "the other source must be persisted as evidence"
            source_ids = {
                str(s.get(db.source_items, str(e.source_item_id)).source_id)
                for e in ev_rows
            }
            assert source_ids == {"bbc-tech", "guardian-tech"}, source_ids

    for te in outcome.decision["trust_evaluations"]:
        assert te["independent_corroboration"] == 2
        assert te["total_evidence"] == 2


# --------------------------------------------------------------------------- #
# No false corroboration: two items in the SAME story but about DIFFERENT events
# must each keep exactly one evidence row and never reach the publish bar.
# --------------------------------------------------------------------------- #
def test_same_story_unrelated_items_stay_single_source():
    with get_session() as s:
        _seed_source(s, id="src-x", tier="TIER_3")
        _seed_source(s, id="src-y", tier="TIER_3")
        item_a = _seed_item(
            s, source_id="src-x",
            title="UK government rejects kill switch idea for dangerous AI",
        )
        item_b = _seed_item(
            s, source_id="src-y",
            title="Google picks Finland for Europe's largest AI data centre investment",
        )

    result = _run([item_a, item_b])

    assert result["stories_detected"] == 1, "both items must share the technology story"
    outcome = result["outcomes"][0]
    bk = outcome.business_key

    with get_session() as s:
        claim_rows = s.query(db.claims).filter_by(story_id=bk).all()
        assert len(claim_rows) == 2
        for claim in claim_rows:
            ev_rows = s.query(db.claim_evidence).filter_by(claim_id=str(claim.id)).all()
            assert len(ev_rows) == 1, "unrelated items must NOT cross-corroborate"

    for te in outcome.decision["trust_evaluations"]:
        assert te["independent_corroboration"] == 1
    assert outcome.final_status != "PUBLISHED"


# --------------------------------------------------------------------------- #
# Later-run regression (#12): a second valid source arriving AFTER the first
# verdict must be incorporated on the next run -- the claim gains a second
# evidence row, the persisted trust evaluation is UPDATED to reflect the new
# corroboration, and the persisted decision follows it (REVIEW -> PUBLISH).
# It must never leave a silently-stale evaluation/decision pair in the DB.
# --------------------------------------------------------------------------- #
def test_later_run_adopts_new_evidence_and_updates_persisted_evaluation():
    from newsforge.verify.persist import run_verification

    story_key = "central_bank_economy_2026"
    with get_session() as s:
        _seed_source(s, id="bbc-econ", tier="TIER_3")
        _seed_source(s, id="guardian-econ", tier="TIER_3")
        item_a = _seed_item(s, source_id="bbc-econ", title=_HEADLINES[0], description=_HEADLINES[0])
        item_b = _seed_item(s, source_id="guardian-econ", title=_HEADLINES[1], description=_HEADLINES[1])
        st = db.stories()
        st.story_id = story_key
        st.slug = story_key
        st.title = _HEADLINES[0]
        session = s
        session.add(st)
        session.flush()
        # Run 1: only the BBC item exists -> single source, single evidence.
        sig_a = db.story_signals()
        sig_a.story_id = story_key
        sig_a.item_id = item_a
        session.add(sig_a)
        session.commit()

    def _verify():
        with get_session() as s:
            return run_verification(
                claims_specs=_default_claim_spec_builder(s, "handle", story_key),
                story_id=story_key,
                reference_time=_T0,
            )

    run1 = _verify()
    assert run1["trust_evaluations"][0]["independent_corroboration"] == 1
    assert run1["trust_evaluations"][0]["total_evidence"] == 1
    assert run1["decision"] == "REVIEW", run1["decision"]

    with get_session() as s:
        claim_row_a = s.query(db.claims).filter_by(story_id=story_key).one()
        assert len(s.query(db.claim_evidence).filter_by(claim_id=str(claim_row_a.id)).all()) == 1
        te_row_a = s.query(db.trust_evaluations).filter_by(target_id=claim_row_a.claim_id).one()
        assert te_row_a.independent_corroboration == 1
        assert s.query(db.decisions).filter_by(target_id=story_key).count() == 1

    # Run 2: the Guardian item joins the same story -> valid cross-source evidence.
    with get_session() as s:
        sig_b = db.story_signals()
        sig_b.story_id = story_key
        sig_b.item_id = item_b
        s.add(sig_b)
        s.commit()

    run2 = _verify()
    assert run2["decision"] == "PUBLISH", run2["decision"]
    assert run2["trust_score"] >= 60
    for te in run2["trust_evaluations"]:
        assert te["independent_corroboration"] == 2
        assert te["total_evidence"] == 2

    with get_session() as s:
        claims_rows = s.query(db.claims).filter_by(story_id=story_key).all()
        assert len(claims_rows) == 2
        for claim in claims_rows:
            n_ev = len(s.query(db.claim_evidence).filter_by(claim_id=str(claim.id)).all())
            assert n_ev == 2, "the valid later source must be persisted as new evidence"
        # Persisted evaluations were UPDATED in place, not left stale.
        for claim in claims_rows:
            te = s.query(db.trust_evaluations).filter_by(target_id=claim.claim_id).one()
            assert te.independent_corroboration == 2
            assert te.total_evidence == 2
        # One decision row only, updated REVIEW -> PUBLISH.
        decision_rows = s.query(db.decisions).filter_by(target_id=story_key).all()
        assert len(decision_rows) == 1
        assert decision_rows[0].decision == "PUBLISH"