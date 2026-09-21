# Phase: Monetization & Automation - Completion Report

**Date:** 2026-09-21  
**Commit:** `ee08f38` (deployed to Render)  
**Deploy ID:** `dep-daoktsv40ujc73fncnhg`  
**Status:** LIVE

## Summary

Extended the verified NewsForge infrastructure with real ad provider abstraction, scheduler integration, editorial quality gate, SEO improvements, and topic pages — all while maintaining the deterministic, non-AI production pipeline.

## What Was Built

### 1. Ad Provider Abstraction (`src/newsforge/ads/__init__.py`)
- `AdProvider` ABC with `resolve_slot()` and `supports()` methods
- `PlaceholderProvider` for dev/staging (returns `AD_SLOT_AVAILABLE` with placeholder HTML)
- `AdSenseProvider` with per-slot ad unit ID resolution via env vars (5 slots: home-banner, home-infeed, home-sidebar, article-banner, article-sidebar)
- `get_provider()` factory reads `NEWSFORGE_AD_PROVIDER` env var
- `render_ad_slot_html()` renders slot with provider-specific ad unit
- `insert_ad_slots()` accepts `provider` param; graceful fallback on provider failure
- 4-layer status tracking: `AD_SLOT_AVAILABLE`, `AD_PROVIDER_LOADED`, `AD_IMPRESSION`, `AD_REVENUE`

### 2. Scheduler (`src/newsforge/pipeline/scheduler.py`)
- APScheduler `BackgroundScheduler` integration
- `start_scheduler()`, `get_scheduler_status()`, `_run_pipeline_job()`
- Protected by existing process-wide lock + 6-layer idempotency
- **SINGLE_PROCESS scope only** — NOT multi-instance safe (documented)
- Controlled by `NEWSFORGE_SCHEDULER_ENABLED`, `NEWSFORGE_SCHEDULER_INTERVAL_MINUTES`, `NEWSFORGE_SCHEDULER_CRON` env vars
- Admin status endpoint: `GET /admin/scheduler/status` (admin-gated)

### 3. Editorial Quality Gate (`src/newsforge/verify/quality.py`)
- `evaluate_editorial_quality()` checks:
  - Word count bounds (configurable min/max, content sections only — excludes boilerplate)
  - Empty body detection (no fact/bullet/event/qa sections)
  - Source attribution presence
- Soft gate in publisher — logs warnings but does NOT block publication
- Integrated into `publish/publisher.py` as soft check

### 4. SEO Improvements
- **RSS feed** (`seo/feeds.py`): `render_rss_xml()` accepts `language` parameter (was hardcoded `en`)
- **Open Graph** (`seo/meta.py`): `open_graph_tags()` emits `article:published_time` when `published_at` provided
- **Sitemap** (`seo/feeds.py`): Entries include `lastmod` key
- **Article template** (`article.html`): Added `<meta name="description">`, `<meta name="robots" content="index, follow">`
- **Base template** (`base.html`): Dynamic `<html lang="{{ lang | default('en') }}">`

### 5. Topic Pages
- `GET /topics/{topic}` route renders topic page with matching articles
- `topic.html` template with article list
- Sitemap includes topic page URLs

### 6. Config Additions (`src/newsforge/config.py`)
- `AdConfig`: provider, adsense_client_id, per-slot adsense_slot_ids (5 env vars)
- `SchedulerConfig`: enabled, interval_minutes, cron_expression
- `QualityConfig`: min_word_count, max_word_count, require_source_attribution

### 7. Requirements
- `APScheduler==3.11.0` added to `requirements.txt`

## Test Results

| Suite | Result |
|-------|--------|
| Full test suite | **541 passed, 15 skipped, 0 failed** |
| New tests (`test_production_reliability.py`) | **19/19 passed** |
| Lint (`ruff check src/`) | Pre-existing warnings only; no new F401/F821 errors |

## Render Endpoint Verification

| Endpoint | Status | Notes |
|----------|--------|-------|
| `GET /live` | 200 | `{"status": "alive"}` |
| `GET /ready` | 200 | `{"status": "ready"}` |
| `GET /health` | 200 | `{"status": "ok", "db": "connected"}` |
| `GET /robots.txt` | 200 | `Allow: /` |
| `GET /sitemap.xml` | 200 | 9 URLs with lastmod |
| `GET /feed.xml` | 200 | RSS 2.0, language=es |
| `GET /topics/{topic}` | 200 | Topic page renders correctly |
| `GET /admin/scheduler/status` | 200 | Enabled=false, scope=SINGLE_PROCESS |
| `GET /articles/{slug}` | 200 | Ad slots, meta description, meta robots, canonical, JSON-LD |

### Article HTML Features Verified
- `ad-slot` markers present in all 5 positions
- `<meta name="description">` present
- `<meta name="robots" content="index, follow">` present
- `<link rel="canonical">` present
- OpenGraph tags present
- JSON-LD structured data present

## Known Limitations (Pre-existing)

1. **`article:published_time`** not showing on some articles because `_article_view` selects the publication with `ORDER BY published_at DESC`, which returns NULL entries first. This is a pre-existing query issue, not introduced by this phase.
2. **Scheduler** is SINGLE_PROCESS scope — will be replaced by external scheduler for multi-instance deployments.
3. **Ad provider** uses PlaceholderProvider by default; AdSense requires real provider IDs via env vars.

## Environment Variables Added

| Variable | Default | Description |
|----------|---------|-------------|
| `NEWSFORGE_AD_PROVIDER` | `placeholder` | Ad provider (`placeholder` or `adsense`) |
| `NEWSFORGE_ADSENSE_CLIENT_ID` | — | AdSense publisher ID |
| `NEWSFORGE_AD_SLOT_HOME_BANNER` | — | AdSense slot ID for home banner |
| `NEWSFORGE_AD_SLOT_HOME_INFEED` | — | AdSense slot ID for home in-feed |
| `NEWSFORGE_AD_SLOT_HOME_SIDEBAR` | — | AdSense slot ID for home sidebar |
| `NEWSFORGE_AD_SLOT_ARTICLE_BANNER` | — | AdSense slot ID for article banner |
| `NEWSFORGE_AD_SLOT_ARTICLE_SIDEBAR` | — | AdSense slot ID for article sidebar |
| `NEWSFORGE_SCHEDULER_ENABLED` | `false` | Enable pipeline scheduler |
| `NEWSFORGE_SCHEDULER_INTERVAL_MINUTES` | `30` | Scheduler interval |
| `NEWSFORGE_SCHEDULER_CRON` | — | Cron expression for scheduler |

## Files Changed

| File | Type | Lines |
|------|------|-------|
| `src/newsforge/ads/__init__.py` | Modified | 187 additions |
| `src/newsforge/config.py` | Modified | 54 additions |
| `src/newsforge/pipeline/scheduler.py` | New | Scheduler module |
| `src/newsforge/publish/publisher.py` | Modified | 21 additions |
| `src/newsforge/seo/feeds.py` | Modified | 4 changes |
| `src/newsforge/seo/meta.py` | Modified | 5 changes |
| `src/newsforge/verify/quality.py` | Modified | 62 additions |
| `src/newsforge/web/app.py` | Modified | 106 additions |
| `src/newsforge/web/templates/article.html` | Modified | 4 changes |
| `src/newsforge/web/templates/base.html` | Modified | 4 changes |
| `src/newsforge/web/templates/topic.html` | New | Topic page template |
| `tests/test_production_reliability.py` | New | 19 tests |
| `.env.example` | Modified | 18 additions |
| `.env.production.example` | Modified | 16 additions |
| `requirements.txt` | Modified | 1 addition |
| `docs/PHASE-MONETIZATION-AUTOMATION-REPORT.md` | New | This report |

## Next Steps

1. Configure real AdSense provider with actual slot IDs when ready
2. Enable scheduler (`NEWSFORGE_SCHEDULER_ENABLED=true`) when ready for automated runs
3. Fix pre-existing `article:published_time` NULL ordering issue in `_article_view`
4. Consider external scheduler (e.g., cron job) for multi-instance deployments
