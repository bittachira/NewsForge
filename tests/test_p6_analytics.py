"""P6 - Analytics & BI gate tests.

Every test runs against a throwaway SQLite database (isolated per test) with its own session.
No row leaks between tests and no `.db` file is reused (§15 isolation). Multi-currency ROI
semantics are enforced: ROI only calculated when revenue and cost are in the same currency;
otherwise status="MULTI_CURRENCY_UNRESOLVED" or "NO_COST_BASELINE".

Run: python -m pytest tests/test_p6_analytics.py -q
"""
from __future__ import annotations
from pathlib import Path
import pytest

from newsforge.analytics import (
    content_roi_query, cost_query, record_revenue_event,
    record_traffic_event, revenue_query, total_ai_cost,
    traffic_query
)
from newsforge.db import (
    decisions, generated_artifacts, get_session, published_snapshots,
    publications, source_items, stories, trust_evaluations,
    quality_evaluations, claims, sources as _sources,
    use_isolated_database_ctx)
from newsforge.db.session import get_session_factory  # Import for isolated DB fixture
from newsforge.db.models import ai_jobs, ai_runs
from newsforge.web.app import create_app


# --------------------------------------------------------------------------- #
# Isolation fixtures + seeding helpers (consistent with existing P3/P4/P5 tests)
# --------------------------------------------------------------------------- #

_db_seq = 0
T1 = "2026-09-01T10:00:00"
T1B = "2026-09-01T12:00:00"
T2 = "2026-09-02T09:00:00"


@pytest.fixture(autouse=True)
def isolated_db():
    """Give every test its own throwaway database so no row can leak between tests.

    Each test opens a UNIQUE file (p6_{seq}.db). A per-test file means one test's
    lingering connection can never corrupt another test's database, and pytest removes
    .pytest_tmp after each test -- we do NOT rely on deleting a file while a connection
    may still be open.

    Uses yield to provide a session that persists for the entire test duration,
    allowing multiple operations within the test to share the same transaction."""
    global _db_seq
    db_dir = Path.cwd() / ".pytest_tmp"
    db_dir.mkdir(exist_ok=True)
    _db_seq += 1
    path = db_dir / f"p6_{_db_seq}.db"
    
    # Ensure fresh database by removing any existing file at this path
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass  # File locked or missing, will recreate when opened

    with use_isolated_database_ctx(path):
        session = get_session_factory()()
        yield session
        session.close()


# --------------------------------------------------------------------------- #
# Seeding helpers (each test calls these to set up its specific scenario)
# --------------------------------------------------------------------------- #

def _seed_story(session, story_id: str, slug: str, title: str = "T", summary: str = "S"):
    """Seed a real Story."""
    st = stories()
    st.id = story_id
    st.story_id = story_id
    st.slug = slug
    st.title = title
    st.summary = summary
    session.add(st)
    session.commit()
    return st


def _seed_traffic(session, entity_id: str, views: int = 0, users: int = 0, recorded_at: str | None = None):
    """Seed traffic events for an entity."""
    return record_traffic_event(
        session, entity_id=entity_id, views=views, users=users, recorded_at=recorded_at
    )


def _seed_revenue(session, entity_id: str, amount: float, currency: str = "USD", recorded_at: str | None = None):
    """Seed revenue events for an entity."""
    return record_revenue_event(
        session, entity_id=entity_id, amount=amount, currency=currency, recorded_at=recorded_at
    )


def _seed_cost(session, artifact_id: str, story_id: str, cost_usd: float,
               tokens_in: int = 100, tokens_out: int = 50, recorded_at: str | None = None):
    """Seed AI cost and generated artifact for a story."""
    D1 = "2026-09-01T10:00:00" if recorded_at is None else recorded_at
    
    job = ai_jobs(
        task_type="GENERATION", model_provider="mock", model_name="deterministic-template",
        tokens_input=tokens_in, tokens_output=tokens_out, latency_ms=1.0, cost_usd=cost_usd,
        status="SUCCESS", created_at=D1
    )
    session.add(job)
    session.flush()
    
    art = generated_artifacts(story_id=story_id, artifact_id=artifact_id, format="ARTICLE",
                              generator_version="p4.v1", template_version="p4.v1")
    session.add(art)
    session.flush()
    
    session.add(ai_runs(job_id=str(job.id), run_id=artifact_id))
    session.commit()


# --------------------------------------------------------------------------- #
# Tests: Multi-currency ROI semantics (core P6 functionality)
# --------------------------------------------------------------------------- #

def test_multi_currency_separation(isolated_db):
    """Verify USD and EUR revenues stay separate; no artificial mixing.

    story-a has: revenue USD=30, revenue EUR=20
    cost_usd=10 (all costs are in USD baseline)

    Expected:
      - USD row: ROI = (30-10)/10 = 2.0, status="OK"
      - EUR row: roi=None, status="MULTI_CURRENCY_UNRESOLVED" (cannot compare to USD cost)
    """
    _seed_story(isolated_db, "story-a", "a")
    
    # Add traffic (views=35 aggregated across periods)
    _seed_traffic(isolated_db, "story-a", views=10, recorded_at=T1)  # period 2026-09-01
    _seed_traffic(isolated_db, "story-a", views=25, recorded_at=T2)  # period 2026-09-02
    
    # Add revenue in two currencies (same date T1 but different currency)
    _seed_revenue(isolated_db, "story-a", amount=30.0, currency="USD", recorded_at=T1)
    _seed_revenue(isolated_db, "story-a", amount=20.0, currency="EUR", recorded_at=T1)
    
    # Add AI cost (always USD baseline)
    _seed_cost(isolated_db, artifact_id="a-ai-job", story_id="story-a", cost_usd=10.0)
    
    rows = content_roi_query(isolated_db)
    
    assert len(rows) == 2, f"Expected 2 rows for multi-currency content, got {len(rows)}"
    
    usd_row = next((r for r in rows if r["currency"] == "USD"), None)
    eur_row = next((r for r in rows if r["currency"] == "EUR"), None)
    
    assert usd_row is not None, "USD row missing"
    assert eur_row is not None, "EUR row missing"
    
    # USD: valid ROI (revenue and cost in same currency)
    assert usd_row["revenue"] == pytest.approx(30.0), f"USD revenue mismatch: {usd_row['revenue']}"
    assert usd_row["cost_usd"] == pytest.approx(10.0), f"USD cost mismatch: {usd_row['cost_usd']}"
    assert usd_row["roi"] == pytest.approx(2.0), f"USD ROI mismatch: {usd_row['roi']} (expected 2.0)"
    assert usd_row["roi_status"] == "OK", f"USD status should be OK, got {usd_row['roi_status']}"
    
    # EUR: cannot compare to USD cost without FX conversion
    assert eur_row["revenue"] == pytest.approx(20.0), f"EUR revenue mismatch: {eur_row['revenue']}"
    assert eur_row["cost_usd"] == pytest.approx(10.0), f"EUR cost_usd field shows USD baseline: {eur_row['cost_usd']}"
    assert eur_row["roi"] is None, f"EUR roi should be None (no FX), got {eur_row['roi']}"
    assert eur_row["roi_status"] == "MULTI_CURRENCY_UNRESOLVED", \
        f"EUR status should be MULTI_CURRENCY_UNRESOLVED, got {eur_row['roi_status']}"


def test_single_currency_roi(isolated_db):
    """Verify ROI works correctly when only one currency exists."""
    _seed_story(isolated_db, "story-a", "a")
    
    _seed_traffic(isolated_db, "story-a", views=35, recorded_at=T1)  # aggregated
    
    _seed_revenue(isolated_db, "story-a", amount=30.0, currency="USD", recorded_at=T1)
    
    _seed_cost(isolated_db, artifact_id="a-ai-job", story_id="story-a", cost_usd=10.0)
    
    rows = content_roi_query(isolated_db)
    
    assert len(rows) == 1, f"Expected 1 row for single-currency content, got {len(rows)}"
    
    r = rows[0]
    assert r["currency"] == "USD", f"Expected USD currency, got {r['currency']}"
    assert r["revenue"] == pytest.approx(30.0), f"Revenue mismatch: {r['revenue']}"
    assert r["cost_usd"] == pytest.approx(10.0), f"Cost mismatch: {r['cost_usd']}"
    assert r["roi"] == pytest.approx(2.0), f"ROI mismatch: {r['roi']} (expected 2.0)"
    assert r["roi_status"] == "OK", f"Status should be OK, got {r['roi_status']}"


def test_multi_currency_same_value_different_currencies(isolated_db):
    """Verify ROI differs based on currency even with same nominal value."""
    _seed_story(isolated_db, "story-a", "a")
    
    _seed_traffic(isolated_db, "story-a", views=35, recorded_at=T1)
    
    # Same nominal amount in different currencies
    _seed_revenue(isolated_db, "story-a", amount=50.0, currency="USD", recorded_at=T1)
    _seed_revenue(isolated_db, "story-a", amount=50.0, currency="EUR", recorded_at=T1)
    
    _seed_cost(isolated_db, artifact_id="a-ai-job", story_id="story-a", cost_usd=20.0)
    
    rows = content_roi_query(isolated_db)
    
    assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
    
    usd_row = next((r for r in rows if r["currency"] == "USD"), None)
    eur_row = next((r for r in rows if r["currency"] == "EUR"), None)
    
    assert usd_row is not None, "USD row missing"
    assert eur_row is not None, "EUR row missing"
    
    # USD: ROI = (50-20)/20 = 1.5
    assert usd_row["roi"] == pytest.approx(1.5), f"USD ROI mismatch: {usd_row['roi']}"
    assert usd_row["roi_status"] == "OK", f"USD status should be OK, got {usd_row['roi_status']}"
    
    # EUR: cannot compare directly to USD cost
    assert eur_row["roi"] is None, f"EUR roi should be None, got {eur_row['roi']}"
    assert eur_row["roi_status"] == "MULTI_CURRENCY_UNRESOLVED", \
        f"EUR status should be MULTI_CURRENCY_UNRESOLVED, got {eur_row['roi_status']}"


def test_multi_currency_one_empty_currency(isolated_db):
    """Verify ROI when only one currency has revenue data."""
    _seed_story(isolated_db, "story-a", "a")
    
    _seed_traffic(isolated_db, "story-a", views=35, recorded_at=T1)
    
    # Only EUR revenue (no USD revenue)
    _seed_revenue(isolated_db, "story-a", amount=70.0, currency="EUR", recorded_at=T1)
    
    _seed_cost(isolated_db, artifact_id="a-ai-job", story_id="story-a", cost_usd=20.0)
    
    rows = content_roi_query(isolated_db)
    
    assert len(rows) == 1, f"Expected 1 row (only EUR), got {len(rows)}"
    r = rows[0]
    assert r["currency"] == "EUR", f"Expected EUR currency, got {r['currency']}"
    assert r["revenue"] == pytest.approx(70.0), f"Revenue mismatch: {r['revenue']}"
    assert r["cost_usd"] == pytest.approx(20.0), f"Cost mismatch: {r['cost_usd']}"
    
    # EUR cannot compare to USD cost baseline without FX
    assert r["roi"] is None, f"ROI should be None for cross-currency, got {r['roi']}"
    assert r["roi_status"] == "MULTI_CURRENCY_UNRESOLVED", \
        f"Status should be MULTI_CURRENCY_UNRESOLVED, got {r['roi_status']}"


def test_multi_currency_without_cost(isolated_db):
    """Verify NO_COST_BASELINE status when no cost exists."""
    _seed_story(isolated_db, "story-a", "a")
    
    _seed_traffic(isolated_db, "story-a", views=35, recorded_at=T1)
    
    _seed_revenue(isolated_db, "story-a", amount=50.0, currency="USD", recorded_at=T1)
    _seed_revenue(isolated_db, "story-a", amount=20.0, currency="EUR", recorded_at=T1)
    
    # No cost assigned
    
    rows = content_roi_query(isolated_db)
    
    assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
    
    usd_row = next((r for r in rows if r["currency"] == "USD"), None)
    eur_row = next((r for r in rows if r["currency"] == "EUR"), None)
    
    assert usd_row is not None, "USD row missing"
    assert eur_row is not None, "EUR row missing"
    
    # Both should have NO_COST_BASELINE since no cost exists
    assert usd_row["roi"] is None and usd_row["roi_status"] == "NO_COST_BASELINE", \
        f"USD should have NO_COST_BASELINE, got {usd_row['roi']}/{usd_row['roi_status']}"
    assert eur_row["roi"] is None and eur_row["roi_status"] == "NO_COST_BASELINE", \
        f"EUR should have NO_COST_BASELINE, got {eur_row['roi']}/{eur_row['roi_status']}"


def test_content_without_revenue_and_cost(isolated_db):
    """Verify no ROI rows when content exists but has no revenue/cost."""
    _seed_story(isolated_db, "story-a", "a")
    
    _seed_traffic(isolated_db, "story-a", views=35, recorded_at=T1)
    
    # No revenue, no cost
    
    rows = content_roi_query(isolated_db)
    
    assert len(rows) == 0, f"Expected 0 rows when no revenue/cost, got {len(rows)}"


def test_aggregation_by_content_across_periods(isolated_db):
    """Verify ROI aggregates traffic across periods correctly."""
    _seed_story(isolated_db, "story-a", "a")
    
    # Period 1
    _seed_traffic(isolated_db, "story-a", views=10, recorded_at=T1)
    # Period 2  
    _seed_traffic(isolated_db, "story-a", views=25, recorded_at=T2)
    
    _seed_revenue(isolated_db, "story-a", amount=10.0, currency="USD", recorded_at=T1B)
    _seed_cost(isolated_db, artifact_id="a-ai-job", story_id="story-a", cost_usd=3.0)
    
    rows = content_roi_query(isolated_db)
    
    assert len(rows) == 1, f"Expected 1 aggregated row, got {len(rows)}"
    r = rows[0]
    # Aggregated views: 10 + 25 = 35
    assert (r["views"], r["revenue"], r["cost_usd"]) == (35.0, 10.0, 3.0), \
        f"Aggregation mismatch: {r['views']}/{r['revenue']}/{r['cost_usd']}"


def test_cost_zero_no_division(isolated_db):
    """Verify cost=0 returns NO_COST_BASELINE, not a division error."""
    _seed_story(isolated_db, "story-a", "a")
    
    _seed_traffic(isolated_db, "story-a", views=35, recorded_at=T1)
    
    _seed_revenue(isolated_db, "story-a", amount=5.0, currency="USD", recorded_at=T1)
    _seed_cost(isolated_db, artifact_id="a-ai-job", story_id="story-a", cost_usd=0.0)
    
    rows = content_roi_query(isolated_db)
    
    assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
    r = rows[0]
    assert r["cost_usd"] == pytest.approx(0.0), f"Cost should be 0, got {r['cost_usd']}"
    assert r["revenue"] == pytest.approx(5.0), f"Revenue mismatch: {r['revenue']}"
    
    # ROI is undefined when cost=0
    assert r["roi"] is None and r["roi_status"] == "NO_COST_BASELINE", \
        f"Should have NO_COST_BASELINE, got {r['roi']}/{r['roi_status']}"


def test_content_without_revenue(isolated_db):
    """Verify content_roi_query returns no rows when there's no revenue data at all."""
    _seed_story(isolated_db, "story-a", "a")
    
    _seed_traffic(isolated_db, "story-a", views=35, recorded_at=T1)
    
    # No revenue recorded at all
    _seed_cost(isolated_db, artifact_id="a-ai-job", story_id="story-a", cost_usd=10.0)
    
    rows = content_roi_query(isolated_db)
    
    # Without any revenue data (neither USD nor EUR), no ROI rows exist
    assert len(rows) == 0, f"Expected 0 rows without revenue, got {len(rows)}"


# --------------------------------------------------------------------------- #
# Tests: Traffic & Revenue queries (independent of ROI semantics)
# --------------------------------------------------------------------------- #

def test_traffic_query_values(isolated_db):
    """Verify traffic_query returns aggregated views/users per entity/period."""
    _seed_story(isolated_db, "story-a", "a")
    _seed_story(isolated_db, "story-b", "b")
    
    # Story A: period 2026-09-01
    _seed_traffic(isolated_db, "story-a", views=150, users=0, recorded_at=T1)  # No users registered
    # Story B: period 2026-09-02 (no views/users - just exists for aggregation test)
    _seed_traffic(isolated_db, "story-b", views=7, recorded_at=T2)
    
    rows = traffic_query(isolated_db)
    
    assert len(rows) == 2, f"Expected 2 traffic rows, got {len(rows)}"
    
    a1 = next((r for r in rows if r["entity_id"] == "story-a" and r["period"] == T1[:10]), None)
    b1 = next((r for r in rows if r["entity_id"] == "story-b"), None)
    
    assert a1 is not None, "Story A traffic row missing"
    assert (a1["views"], a1["users"]) == (150.0, 0.0), f"Traffic mismatch: {a1['views']}/{a1['users']}"
    
    assert b1 is not None, "Story B traffic row missing"
    assert (b1["period"], b1["views"], b1["users"]) == (T2[:10], 7.0, 0.0), \
        f"B traffic mismatch: {b1['period']}/{b1['views']}/{b1['users']}"


def test_revenue_query_values(isolated_db):
    """Verify revenue_query returns revenues separated by currency."""
    _seed_story(isolated_db, "story-a", "a")
    
    _seed_traffic(isolated_db, "story-a", views=35, recorded_at=T1)  # for aggregation
    
    _seed_revenue(isolated_db, "story-a", amount=30.0, currency="USD", recorded_at=T1)
    _seed_revenue(isolated_db, "story-a", amount=20.5, currency="EUR", recorded_at=T1)
    
    rows = revenue_query(isolated_db)
    
    assert len(rows) == 2, f"Expected 2 revenue rows (USD + EUR), got {len(rows)}"
    
    usd_d1 = next((r for r in rows if r["currency"] == "USD" and r["period"] == T1[:10]), None)
    eur_d1 = next((r for r in rows if r["currency"] == "EUR"), None)
    
    assert usd_d1 is not None, "USD revenue row missing"
    assert usd_d1["revenue"] == pytest.approx(30.0), f"USD revenue mismatch: {usd_d1['revenue']}"
    
    assert eur_d1 is not None, "EUR revenue row missing"
    assert eur_d1["revenue"] == pytest.approx(20.5), f"EUR revenue mismatch: {eur_d1['revenue']}"


def test_cost_query_from_real_ai_jobs(isolated_db):
    """Verify cost_query reads from ai_jobs table (costs are always in USD)."""
    _seed_story(isolated_db, "story-a", "a")
    
    # Generate with specific cost
    _seed_cost(isolated_db, artifact_id="a-ai-job", story_id="story-a", cost_usd=0.42)
    
    rows = cost_query(isolated_db)
    total = total_ai_cost(isolated_db)
    
    assert len(rows) == 1, f"Expected 1 cost row, got {len(rows)}"
    assert sum(r["cost_usd"] for r in rows) == pytest.approx(0.42), \
        f"Total cost mismatch: {sum(r['cost_usd'] for r in rows)}"
    assert total == pytest.approx(0.42), f"total_ai_cost() mismatch: {total}"


# --------------------------------------------------------------------------- #
# Tests: Determinism & Edge cases
# --------------------------------------------------------------------------- #

def test_determinism_and_idempotent_events(isolated_db):
    """Verify identical events produce identical results (idempotency)."""
    _seed_story(isolated_db, "story-a", "a")
    
    r1 = traffic_query(isolated_db)
    r2 = traffic_query(isolated_db)
    
    assert r1 == r2, f"Traffic queries not deterministic: {r1} != {r2}"


def test_empty_database(isolated_db):
    """Verify analytics queries return empty on fresh database with only seed_story."""
    _seed_story(isolated_db, "story-a", "a")  # Story exists but no analytics data
    
    rows_traffic = traffic_query(isolated_db)
    rows_revenue = revenue_query(isolated_db)
    rows_cost = cost_query(isolated_db)
    rows_roi = content_roi_query(isolated_db)
    
    assert rows_traffic == [], "Traffic should be empty"
    assert rows_revenue == [], "Revenue should be empty"
    assert rows_cost == [], "Cost should be empty"
    assert rows_roi == [], "ROI should be empty (no cost)"