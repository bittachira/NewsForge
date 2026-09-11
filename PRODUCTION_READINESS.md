# Production Readiness — NewsForge

Status: **configuration & gates verified in staging; NO production deployment
executed yet.**

This document defines what NewsForge requires to run in production, what the
application itself enforces (fail-fast gates, shipped and CI-verified), and what
remains operator action. A production deployment has **not** been performed — the
deployment target is `UNSELECTED` and nothing is pre-allocated.

---

## 1. Prerequisites

- PostgreSQL server reachable from the application (managed service or your own).
- A place to run the container (deployment target provider — *not selected yet*).
- A secret delivery mechanism. **The only mechanism implemented is the
  environment** (`secret_source()` = `"env"`): platform env vars or a secret
  provider that injects env vars. There is no Vault/KMS client yet, because no
  infrastructure has been chosen.
- `pg_dump` / `pg_restore` available wherever backups run (PostgreSQL client
  tools), and durable storage for `/data` (database + offline backups).

## 2. Environment

The single source of truth is `NEWSFORGE_ENVIRONMENT`. Only the literal value
`production` triggers the fail-fast gates; `development` / `test` / `staging`
are never gated and keep their current behaviour.

A production process **refuses to start** when any of the following holds
(`validate_production_config` → `assert_production_safe`, verified by the test
suite and exercised at startup before serving):

| Gate | Env contract |
|------|--------------|
| Database | `NEWSFORGE_DATABASE_URL` set and `postgresql://…` (SQLite refused) |
| Admin | `NEWSFORGE_ADMIN_TOKEN` set (strong random value) |
| AI | `NEWSFORGE_MOCK_AI=false` **and** an explicit real provider |
| AI provider | `NEWSFORGE_DEFAULT_PROVIDER` ∈ `openai\|lm_studio\|ollama` (never `mock`) |
| AI key | `NEWSFORGE_OPENAI_API_KEY` required when provider is `openai` |
| AI tuning | `NEWSFORGE_AI_TIMEOUT_S` > 0, `NEWSFORGE_AI_MAX_TOKENS` > 0 |
| Debug | `NEWSFORGE_DEBUG=false` (true is refused) |
| Public URL | `NEWSFORGE_SITE_URL` set, public, https (localhost refused) |

Use `.env.production.example` as the template. Committed examples contain only
`CHANGE-ME` placeholders; a CI step scans `Dockerfile`, the `.env*` templates and
the source tree for hardcoded secret patterns.

## 3. Secrets

Rules (absolute, enforced by redaction + tests):

- Only delivered via environment / secret provider. Never hardcoded, never
  committed, never baked into the Docker image (the image has no `.env`, and CI
  inspects image layers).
- Never written to structured logs, exception text, error tracking rows
  (`errors` table), metrics labels, or HTTP responses.
- Redaction covers: API keys (OpenAI `sk-…`, GitHub `ghp_…`, AWS `AKIA…`,
  Google `AIza…`, Slack tokens, generic `api_key=`/`apikey:`), bearer tokens,
  `Authorization` headers, cookies, `password`/`passwd`/`pwd` assignments,
  DSNs in free text, and — on top of format patterns — the **exact value** of
  every currently configured secret (`*KEY/*TOKEN/*SECRET/*PASSWORD` env vars
  and DSN passwords). `get_secret(name)` is the single read path;
  `missing_production_secrets()` lists what is absent.

## 4. Database

Production uses only PostgreSQL via `NEWSFORGE_DATABASE_URL`
(`postgresql+pg8000://…`). The runtime dependency set ships the pure-Python
`pg8000` driver, so no `libpq` is needed.

- SQLite is **refused** whenever `NEWSFORGE_ENVIRONMENT=production` — the config
  gate rejects it before any connection, and the startup path double-checks the
  engine dialect is PostgreSQL.
- `create_all()` is a development/test-only mechanism. Production startup never
  silently mutates schema — it runs the migration chain explicitly and then
  validates parity (see Migrations).
- Persistent: the database itself. Backup artefacts live under
  `NEWSFORGE_BACKUP_DIR` (default `/data/backups`). Secrets are never stored in
  `/data`.

## 5. Migrations

Deploy contract (enforced by `init_production_db`):

```
application → validate configuration → connect PostgreSQL →
             upgrade head (Alembic) → migration gate → serve
```

Never `application → silently mutate schema`.

The **migration gate** (`assert_schema_migrated`) fails fast when the on-disk
revision does not equal the head revision shipped in this build, and the schema
boundary refuses newer *or* older `schema_version` markers (a downgraded/upgraded
DB can never serve silently). Characterised states (all CI/test-verified):

- **Current schema** — parked at head → starts.
- **Outdated schema** (un-migrated legacy DB) — adopted (stamped 0001) then
  upgraded to head, explicitly, before serving.
- **Missing migration** (DB revision ≠ head, or an unknown revision) — fail-fast.
- **Incompatible migration** (newer schema than this build) — fail-fast,
  `SchemaIncompatibleError`.

Rollback compatibility: because the schema boundary and revision parity run at
startup, deploying an **older application** against a **newer database** is a
visible fail-fast error — the platform refuses rather than corrupts.

## 6. Startup & health

Startup order in production:

1. `assert_production_safe()` — configuration gates.
2. Connect PostgreSQL (`SELECT 1`).
3. `upgrade head` via Alembic.
4. `assert_schema_migrated()` — revision parity + schema boundary.
5. Serve; `/ready` confirms readiness.

Probes:

| Endpoint | Contract |
|----------|----------|
| `/live` | static 200 — process is alive |
| `/health` | 200 (ok) or 503 (generic body); never leaks internal text |
| `/ready` | 200 when DB reachable; 503 with fixed literals otherwise |
| `/metrics` | **internal** — requires admin token; fail-closed; never leaks secrets |
| `/analytics` | **internal** — same gate |

## 7. Backups (PostgreSQL)

Policy (default; a managed backup with equal properties is acceptable):

- **Mechanism**: nightly `pg_dump --format=custom --compress=9 --no-owner
  --no-privileges` via the `PostgresBackupProvider` (same code path CI verifies),
  written to `NEWSFORGE_BACKUP_DIR`.
- **Frequency**: daily. **Retention**: 14 daily + 4 weekly snapshots (operator
  purge policy; not yet automated).
- **Verification**: every backup is run through `pg_restore --list` immediately
  after creation (`verify()`).
- **Restore procedure**:
  `pg_restore --list <backup>` (prove readable) → create target DB →
  `pg_restore --no-owner --no-privileges -d <target> <backup>` → run the
  application migration gate against the restored DB.

**Backup states are distinct and tracked explicitly:**

- **Backup disponible** — a `pg_dump` archive exists and `verify()` passed.
  *(Achieved in staging for SQLite/PG paths; scheduled SQL jobs are NOT
  implemented — operator action required.)*
- **Backup probado** — `verify()` (`pg_restore --list`) succeeds. *(Implemented
  and tested.)*
- **Restore probado** — an actual restore into a clean database has been executed
  and the restored DB passed the migration gate. *(NOT executed yet for
  PostgreSQL — scheduled as a pre-production drill; this stays an explicit
  blocker until done.)*

Secrets never appear in backup paths, error messages or the verification output
(DSNs are redacted).

## 8. AI in production

- `NEWSFORGE_MOCK_AI=false` is mandatory; the startup gate refuses `true`.
- A **belt-and-suspenders guard** also protects direct use: constructing an
  `AiRouter` with `mock=True` under `NEWSFORGE_ENVIRONMENT=production` raises.
- A real provider must be selected explicitly (`openai | lm_studio | ollama`);
  `openai` additionally requires `NEWSFORGE_OPENAI_API_KEY`. Timeout and max
  tokens must be positive.
- Failure handling: a real-provider failure raises `ProviderError` immediately —
  **there is no silent fallback to MOCK**. Every failure is logged (redacted),
  counted in metrics and persisted to `errors` (sanitized).
- Tests never call real APIs — MOCK/fakes only.

## 9. Admin access

Single, documented admin mechanism (no full RBAC yet): internal endpoints
`/analytics` and `/metrics` require `NEWSFORGE_ADMIN_TOKEN` via `X-Admin-Token`
header or `?token=` query parameter, compared in constant time. **Fail closed**:
no token configured → endpoint closed; wrong token → 403; correct → 200. `?token=`
is accepted for operator convenience; the token is never echoed into responses or
logs.

## 10. Network / HTTP

- HTTPS is terminated in front of the application (proxy/CDN/load balancer of the
  operator's choosing); the container listens on HTTP internally.
- The application does **not** trust forwarded headers for any security decision
  (`X-Forwarded-*` are ignored; host/URL base comes from `NEWSFORGE_SITE_URL`).
- FastAPI auto-docs are disabled (`/docs`, `/redoc`, `/openapi.json` → 404).
- No reverse proxy is implemented yet — none is pre-selected; document yours in
  the deployment record below.

## 11. Storage

| Path | Type | Contents |
|------|------|----------|
| PostgreSQL | **persistent** | all platform data |
| `/data` (= volume) | **persistent** | DB when self-hosted; offline backups |
| `/data/backups` | **persistent** | `pg_dump` / SQLite snapshots (no secrets) |
| `/tmp` (container) | **ephemeral** | scratch |
| `/app` (container) | **read-only** in production; the image also supports
  writable `/app` for in-container CI tests | code + frozen deps |
| application memory | **ephemeral** | metrics, caches |

Secrets are env-only; nothing secret is ever written under `/data` or `/app`.

## 12. Container

Verified in CI (staging image, `runtime` target):

- **Non-root** — runs as `newsforge` (uid 10001); CI asserts image user and live
  `id`.
- **No test dependencies** — `runtime` ships only the frozen runtime deps;
  `pytest` etc. live in the separate `test` overlay target.
- **No build tools** — compilers exist only in the `builder` stage.
- **Explicit port & command** — `EXPOSE 8000`, `CMD ["uvicorn",
  "newsforge.web.app:app", "--host", "0.0.0.0", "--port", "8000"]`.
- **No `.env` baked** — configuration is env-only; CI inspects layers + files.
- **`/data` writable** by the runtime user; `/app` may be mounted read-only in
  production (CI verifies a `--read-only` start with a `/data` volume).
- Deterministic identity — `NEWSFORGE_GIT_COMMIT`/`VERSION`/`BUILD_TIME` are
  build ARGs surfaced as env; CI asserts they equal the commit.

## 13. Deployment target

`PRODUCTION_DEPLOYMENT_TARGET = "UNSELECTED"`.

No provider/region/compute has been chosen. When a target is selected, the record
below is filled with concrete values (nothing is invented or pre-allocated):

| Field | Value |
|-------|-------|
| provider | — |
| region | — |
| compute | — |
| PostgreSQL | — |
| storage | — |
| secret mechanism | `env` (implemented) |
| domain | — |
| TLS | — |
| rollback | — |

## 14. Rollback

- **Previous image**: the previous commit's image (`docker tag <previous-sha>`,
  redeploy). `NEWSFORGE_GIT_COMMIT` identifies the running build.
- **Previous application version**: redeploy the previous build; the migration
  gate + schema boundary make an old app against a new DB fail visibly instead of
  corrupting data.
- **Migration compatibility**: migrations are additive and running them never
  destroys data; on rollback the app refuses if the DB is newer. There is no
  destructive downgrade step.
- **Procedure**: (1) confirm the current DB revision equals the rollback target's
  head; (2) deploy the previous image; (3) run the deployment smoke test; (4) keep
  the newer image available for re-promotion.

`rollback` field of the target record will name the concrete mechanism once a
provider exists.

## 15. Smoke tests (post-deployment)

`scripts/deploy_smoke.py` encodes the contract and runs explicitly (manual
promotion), never automatically against production:

- public: `/live`, `/ready`, `/health`, `/articles`, `/sitemap.xml`, `/feed.xml`
  → 200.
- internal: `/metrics` anonymous → 403 (fail-closed); `/metrics` with
  `NEWSFORGE_ADMIN_TOKEN` → 200 and the token is **not** present in the body.

Usage `NEWSFORGE_SMOKE_BASE_URL=… NEWSFORGE_ADMIN_TOKEN=… python
scripts/deploy_smoke.py`.

## 16. Promotion process (CI/CD gate)

Conceptual pipeline — **no automatic production deployment is enabled**:

```
staging verification green → manual production approval → deploy → smoke → serve
```

Production is blocked when any of: tests fail, migrations are incompatible,
secrets are missing, or the production config is invalid. The staging workflow
(`.github/workflows/staging-verification.yml`) runs the full offline gate suite
including `tests/test_production_security.py` as an explicit step, plus the live
PostgreSQL compatibility suite. A real deployment job is **not** added until a
target is selected.

## 17. REMAINING PRODUCTION BLOCKERS

- **Deployment target unselected** (`PRODUCTION_DEPLOYMENT_TARGET=UNSELECTED`).
- **Restore drill not executed** for PostgreSQL (backup + verify are implemented
  and tested; a clean restore into a fresh DB is scheduled as a pre-production
  run).
- **Automated backup scheduling/retention** not wired (operator cron/managed
  backup).
- **HTTPS/domain/TLS** not provisioned (depends on target selection).