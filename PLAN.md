# PLAN — NewsForge

> AI-native media & information company. Discover → Verify → Transform → Distribute trusted information across channels, build an owned audience and monetize it through many sources. Independent, original, not a content farm.

**Status:** v0.3 — Source Engine (**P1**) and Story Engine (**P2**) complete; starting Trust + Quality Gate (**P3**).
**Date:** 2026-09-05 · **Owner:** autonomous engineering team (CTO-led).

### Progress log
| Phase | State | Verification evidence |
|-------|-------|----------------------|
| P0 — scaffold, config, core, 27-table schema | ✅ committed `44a64e2` | `import newsforge` OK; all 27 tables created; JSON round-trip + relationships verified. |
| P1 — Source Engine (ingest/parse/dedupe/trust) | ✅ committed `99781a3` | Live ingest of BBC News RSS → **30 items** normalized, dedupe confirmed on re-ingest (`added:0 skipped:30`), trust tiers assigned. **8/8 pytest gate tests pass**, deterministically across repeated runs (no leftover artifacts). |
| P2 — Story Engine (detect persistent stories) | ✅ committed (this session) | 10/10 gate tests pass deterministically (3× repeated runs); unrelated signals never merge; idempotent upsert+link verified; story trust aggregates from source tiers. See commit below. Claim engine deferred to P3. |
| P3–P6, MVP DONE | ⬜ roadmap (§4/§6) | See phase table below. |

### P2 — Story Engine: decisions & post-mortem of the untested detector

The handoff flagged `stories/detector.py` as **written but not yet tested**. I verified it before building on top, found two real bugs, and fixed them rather than shipping broken clustering:

1. **`classify_topic` mislabeled unrelated text.** With `best_score = -1`, the first taxonomy entry won even with 0 keyword hits, so "Otra historia totalmente distinta" was tagged `package_tax`. Fixed: non-matching text now returns `("", 0.0)`; multi-word keywords match on any constituent word (better ES/EN recall).
2. **`cluster_items` merged unrelated items.** It computed the entity prefix *per current item* and forced every item through a topic name, so unrelated signals collapsed into one story. Fixed: two-pass clustering — group by `(topic, year)`, then resolve a shared entity prefix only when it genuinely appears in ≥2 distinct items.

**Added `db/story_signals`** (many-to-many join linking source items → their persistent story; unique constraint keeps re-detection idempotent). Registered the new table in `newsforge.db.__init__`.

**Reliability fix.** The first engine pass opened/closed many short-lived sessions per operation. On this Windows+SQLite/QueuePool setup that made commits unreliable (data created but not persisted across sessions) and was flaky across runs. Consolidated to **one session per `process()` call**. Verified deterministic over 3× repeated runs.

**Trust aggregation (§9 MVP proxy).** Story trust = mean of contributing items' source *tier baselines* (TIER_1→95 … TIER_4→35). Deterministic and testable; freshness/consensus/primary-source weighting layered on in P3 without changing this API.

**Deferred (per handoff decision):** LLM-based CLUSTER step (§4) — kept the deterministic heuristic so P2 is testable offline; an LLM swap later changes only `classify_topic` and callers keep working. Claim engine, quality gate, AI editor follow in P3–P4.

### P3 — Trust + Claim + Quality Gate + Decision Engine (in progress)

**Goal:** make it *impossible* for low-trust / high-risk content to auto-publish. Evaluation flows STORY → SOURCE → CLAIM → EVIDENCE → CONTRADICTION → FRESHNESS → RISK → QUALITY → DECISION.

**Design principles:** deterministic, explainable, reproducible, versioned; no LLM as authority for TRUST/RISK/QUALITY/PUBLISH/REJECT/WAIT. Pure functions first (unit-testable), thin persistence layer. Reuse existing enums (`ClaimStatus`, `DecisionState`, `HumanLoopVerdict`) and config (`TrustConfig`, `DecisionConfig`).

**Database (§17):** extend `claims` with `story_id` + `source_item_id` provenance; add 5 tables — `claim_evidence` (unique `(claim_id, source_item_id)` → independent-source corroboration), `trust_evaluations`, `quality_evaluations`, `decisions` (upsert key `(target_type, target_id)` → idempotent + audit with reasons_json/policy_version), `review_tasks` (human queue lifecycle). All new FKs/indexes added; no historical migrations touched.

**Modules (`src/newsforge/verify/`):** `claims.py`, `corroboration.py`, `freshness.py`, `trust.py`, `risk.py`, `quality.py`, `decide.py`.

**Decision rules (§10):** RED+unsupported → REJECT; CONTRADICTED/conflicting evidence → WAIT/REVIEW; not-quality-passed → REVIEW/REJECT; GREEN+SUPPORTED+≥1 independent corroboration+quality pass → PUBLISH. RED / contradiction / missing-critical-evidence can never silently PUBLISH.

---

## 0. Brand & naming decisions (configurable)

| Role | Name | Notes |
|------|------|-------|
| Company / product engine | **NewsForge** | The autonomous information engine that forges stories from verified data. |
| Public media brand (default) | **Lumen** | "Verified light on what matters." Short, premium, works in ES/EN/PT. Fully configurable via `config.py` — rename is a one-line change. |

Everything below can be reconfigured without code changes through the config module.

---

## 1. Guiding principles (from the brief)

Priority order we build to: **utility → trust → accuracy → speed → originality → UX → distribution → owned audience → monetization → automation → scalability.**

Hard rules baked into every module:
- The AI never fabricates information, sources, quotes or figures. It only synthesizes what is provided and flags uncertainty.
- If there isn't enough verified information to publish, the system says **"Información insuficiente para publicar."** — it does not guess.
- No content farm. Every artifact must add value beyond the original source (new data, context, analysis, visualization, tooling).
- Clear separation of **FACTS / ANALYSIS / OPINION**. Editorial vs Sponsored vs Affiliate vs Advertising are always labeled.

---

## 2. Architecture decision record (ADR-001)

**Stack: Python-centric, FastAPI + Jinja2 SSR + SQLite-first.**

### Why this stack
1. **Orchestration & data fit Python natively.** Source ingestion (`aiohttp`), claim extraction, trust scoring and the autonomous pipeline are all far more natural in Python with the libraries already present in the environment.
2. **SEO without a second framework.** We server-render HTML with Jinja2 (SSR). Top news sites are server-rendered; this satisfies SSR/SEO/Core Web Vitals with minimal JavaScript — no dual-framework sync problem.
3. **SQLite-first, Postgres-ready.** Zero-config MVP that runs today and migrates to PostgreSQL later by swapping the SQLAlchemy dialect (`create_engine`). Schema is designed for relational integrity (stories → articles → claims → sources).
4. **Model Router abstraction** decouples AI from any provider: OpenAI-compatible APIs, LM Studio / Ollama local servers, and a deterministic **mock mode** so the whole pipeline runs and tests without an API key or network.

### Explicitly rejected (and why)
- **Next.js/React SPA:** great for marketing sites, but adds a second framework to keep in sync with the data backend during MVP; SSR/SEO is fully achievable server-side here. Revisit if we need client-side interactivity at scale.
- **PostgreSQL from day one:** SQLite gives us a working system immediately; Postgres migration is a dialect swap + index tuning, not a rewrite.
- **Hardcoding any API key / provider:** keys live only in `.env` (gitignored). The router can change providers without touching the pipeline.

### High-level diagram

```
                 ┌───────────────────────────────────────────────┐
   Sources       │  SOURCE ENGINE  (RSS/API/official/gov/sci)     │
   (Tier1-4) ───▶│         sources.engine + sources.trust          │
                 └───────────────┬───────────────────────────────┘
                                 ▼
                    ┌──────────────────────────────┐
                    │  STORY ENGINE (persistent)    │
                    │  stories.detector             │
                    └───────────────┬──────────────┘
                                   ▼
                 ┌──────────────────────────────────────┐
                 │ CLAIM ENGINE + KNOWLEDGE GRAPH        │
                 │ claims.engine · entities · rels       │
                 └───────────────┬──────────────────────┘
                                 ▼
              ┌───────────────────────────────────────────┐
              │ TRUST ENGINE  (source quality, consensus)  │
              │ QUALITY GATE (anti-slop / grammar / SEO)   │
              │ CONTENT DECISION ENGINE (publish/wait/…)   │
              └───────────────┬───────────────────────────┘
                              ▼
                 ┌──────────────────────────────┐
                 │ MODEL ROUTER + AI EDITOR      │
                 │ ai.router · ai.cost · generator│  (OpenAI/LMStudio/Ollama/MOCK)
                 └───────────────┬──────────────┘
                              ▼
              ┌──────────────────────────────────────┐
              │ SEO LAYER: SSR HTML · JSON-LD ·       │
              │ sitemaps · RSS · canonical · OG/Twitter│
              └───────────────┬──────────────────────┘
                              ▼
                 ┌──────────────────────────────┐
                 │ CMS / PUBLISH (web + admin)   │
                 │ ANALYTICS + BI dashboard      │
                 └──────────────────────────────┘
```

---

## 3. Technology stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Language | Python 3.14 | Data/AI/web libs already installed; best fit for the pipeline. |
| Web/API | FastAPI + Pydantic v2 | Async, fast, auto docs (`/docs`), type-safe. |
| Server | uvicorn[standard] | ASGI production server with worker class. |
| Templates | Jinja2 (SSR) | SEO-friendly server-rendered HTML, minimal JS. |
| DB | SQLite 3 (MVP) → PostgreSQL | Zero-config now; dialect swap later. |
| ORM | SQLAlchemy 2.0 style | Relational integrity across entities. |
| Ingestion | aiohttp + BeautifulSoup4 | Async RSS/API fetch without scraping ToS violations. |
| Search/discovery | duckduckgo-search | Read-only discovery of public signals (no API key). |
| Geo/IP | geoip2 | Audience/context enrichment (MaxMind optional key). |
| Crypto | cryptography | Secret management, hashing, signed tokens. |
| AI | Model Router (OpenAI-compatible) + local (LM Studio/Ollama) + MOCK | Provider-independent; runs without keys. |
| Tests | pytest | Unit / integration / pipeline tests gate completion. |

---

## 4. Modules (MVP scope first, then phases)

### MVP — must work end-to-end now (§45)
1. `sources/` — Source Engine: ingest RSS/API items, normalize, assign Tier/trust score, dedupe.
2. `stories/` — Story Engine: detect persistent stories from items (`STORY_ID`).
3. `claims/` — Claim Engine: extract claims per article with states + provenance.
4. `verify/` — Trust Engine (source consensus) + Quality Gate (anti-slop, grammar, SEO, legal risk).
5. `decide/` — Content Decision Engine (REJECT/WAIT/DRAFT/REVIEW/PUBLISH/UPDATE/ARCHIVE) with 7-dimension score + HUMAN-IN-THE-LOOP RED gating.
6. `ai/` — Model Router + AI Cost engine + AI Editor generator (pluggable; MOCK default).
7. `seo/` — JSON-LD, sitemaps, RSS, canonical, OpenGraph/Twitter cards.
8. `web/` — FastAPI app factory: home feed, article page, story control center, admin read endpoints.
9. `pipeline/` — Autonomous orchestrator implementing the 14-phase pipeline with retry/backoff/dead-letter/idempotency/audit logs.
10. `analytics/` — Counters + BI queries (traffic/users/revenue/cost/content ROI).

### Phase 2 (after MVP verified)
Newsletter engine · Social distribution · Affiliate engine · Ad slots · Tools/calculators/comparators · Knowledge graph UI · Personalization · Experimentation (A/B) · Revenue optimization.

### Phase 3+
Story dashboards v2, multi-language (ES/EN/PT → FR/DE/IT), video scripts, premium reports/events, full Story Control Center, RBAC admin, observability/alerts, CI/CD.

---

## 5. Dependencies

**Runtime:** FastAPI, uvicorn[standard], pydantic v2, SQLAlchemy 2.0, python-dotenv, jinja2, aiohttp, beautifulsoup4, duckduckgo-search, geoip2, cryptography, httpx. *(lxml dropped — no C++ toolchain; using bs4's html.parser.)*

**Dev:** pytest (added in Phase 1 dev tooling).

**Optional / external (never hardcoded):** OpenAI-compatible API key, LM Studio/Ollama local server, MaxMind GeoIP key. All optional — the system runs fully offline via MOCK mode.

---

## 6. Phases & verification gates

| Phase | Deliverable | Verification gate (must pass to continue) |
|-------|-------------|--------------------------------------------|
| P0 | Config + DB schema + core logger/security | `python -c "import newsforge"`; DB tables created; unit tests green. |
| P1 | Source Engine + ingest demo | Ingest a real RSS feed → N normalized items, dedupe works, trust tiers assigned. |
| P2 | **Story Engine (done)** — detect persistent stories, link items, story-level trust | One source item → one stable `STORY_ID`; related signals cluster; idempotent upsert+link verified; story trust aggregates from source tiers. Claim engine moved into P3. |
| P3 | Trust + Quality Gate + Decision | A low-trust/contradicted input is **REJECT/WAIT**, not auto-published. |
| P4 | AI Router (MOCK) + Editor | Pipeline produces an original article from claims; no fabricated facts; cost recorded. |
| P5 | SEO + CMS/Publish | Published page has JSON-LD, canonical, OG tags, in sitemap + RSS; `/articles` renders. |
| P6 | Analytics dashboard | BI query returns rows for traffic/revenue/cost/content ROI. |
| **MVP DONE** | End-to-end run on seeded sources | Full pipeline publishes ≥1 verified article with SEO markup; all critical tests pass. |

---

## 7. Risks & mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| No C++ toolchain (lxml) | Medium | Use bs4 html.parser; no lxml dependency. |
| Python 3.14 library compat | Medium | Loose version ranges; verified imports on install. |
| LLM hallucination / fabrication | **Critical** | Generator only emits provided facts + explicit uncertainty; hard anti-slop gate rejects weak content. |
| Over-reliance on Google Search/Ads | High (brief §2) | Multi-source revenue design from day one; owned audience via newsletter/push/membership. |
| Single provider lock-in | Medium | Model Router abstracts providers; MOCK mode for dev/tests. |
| SQLite concurrency at scale | Low-Med now | Read-heavy news workload is fine on SQLite MVP; migrate to Postgres before scale (dialect swap). |
| Silent autonomous changes | High (§49) | All policy changes are logged and require approval when significant. |

---

## 8. Cost estimates (order of magnitude, monthly at MVP scale)

- **AI inference:** near-zero in MOCK mode; with a small LLM model ~$0.10–2 /k generated articles depending on provider/model. We track `ai_jobs` to compute AI cost per article/user/revenue and route cheap models to bulk tasks (see §30/31).
- **Infrastructure:** SQLite + uvicorn on a single box ≈ $0 at MVP; scale to managed Postgres + CDN later (~$20–60/mo).
- **External data:** RSS/APIs are free/public. GeoIP optional (~$0–50 for MaxMind paid tiers). No scraping costs (we respect ToS).
- **Principle:** never depend on one revenue source; measure `AI_COST_PER_ARTICLE` vs `revenue_per_article` to keep the unit economics positive (§48, §30).

---

## 9. Success criteria (definition of done for MVP)

1. ✅ The full pipeline runs autonomously from a real RSS/API source to a published, SEO-optimized article.
2. ✅ Trust + quality gates actually **block** low-confidence or RED-topic content (no auto-publish of rumors/politics/self-harm).
3. ✅ No fabricated facts: generated articles contain only sourced claims; uncertainty is explicit.
4. ✅ Anti-slop rejects repetitive/superficial/generated-only content.
5. ✅ Published pages expose JSON-LD, canonical, OpenGraph and appear in sitemap + RSS.
6. ✅ AI cost is measured per article/user/revenue; model routing is provider-agnostic with a working MOCK fallback.
7. ✅ All critical tests pass (unit + integration + pipeline). No project is "finished" while these fail (§43).

---

## 10. How to run

```bash
cd F:/Noticias
python -m venv .venv && source .venv/Scripts/Activate.ps1   # if not using system env
pip install -r requirements.txt
export NEWSFORGE_DB_PATH=data/newsforge.db        # optional; default data/newsforge.db
export NEWSFORGE_MOCK_AI=1                         # deterministic AI (default) or set a provider key
python -m newsforge run            # start server on http://localhost:8000  (/docs for API)
```

See `README.md` (Phase 1), `ARCHITECTURE.md`, and the module docs under `src/newsforge/`.
