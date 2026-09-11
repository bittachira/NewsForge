# NewsForge on Render — Production Architecture & Deployment Preparation

> Phase: `RENDER_DEPLOYMENT_PREPARATION`. **No resources are provisioned, no money
> is spent, no deploy is executed in this phase.**
>
> ```text
> PROVIDER=Render
> REGION=Europe
> POSTGRESQL=Render Postgres 16.x
> SECRETS_METHOD=Render Environment Variables / Secret Management
> DOMAIN=NOT_SELECTED
> ```
>
> Preparation status (this phase's termination criteria):
>
> ```text
> RENDER_ARCHITECTURE=READY
> WEB_SERVICE_SPEC=READY
> POSTGRES_SPEC=READY
> SECRETS_SPEC=READY
> MIGRATION_SPEC=READY
> STORAGE_SPEC=READY
> BACKUP_SPEC=READY
> ROLLBACK_SPEC=READY
> SMOKE_TEST_PLAN=READY

> PRODUCTION_RESOURCES=NOT_PROVISIONED
> PRODUCTION_DEPLOYMENT=NOT_EXECUTED
> MONEY_SPENT=NONE
> DOMAIN=NOT_SELECTED
> ```
>
> Every item marked **[OWNER]** is a manual action on the owner's Render account.
> No secrets appear in this file; secret *names* only.

---

## 1. Architecture overview

```
Internet
  ↓  HTTPS (Render-managed TLS, auto cert)
Render edge (web service load balancer, HTTP→HTTPS redirect toggle)
  ↓
NewsForge Web Service (Docker runtime — the commit-pinned runtime image)
  ↓  internal URL (same account + region, Render private network)
Render Postgres 16.x
  ↓
NewsForge container
  ↓
Render Disk mounted at /data   (backups + artifacts; NOT the RDBMS)
```

| Component | Render product | NewsForge requirement |
|-----------|----------------|------------------------|
| App runtime | **Web Service (Docker)** | `runtime` stage of the repo Dockerfile; non-root; bind `0.0.0.0`; production env |
| Database | **Render Postgres (paid)** | PostgreSQL 16.x; `NEWSFORGE_DATABASE_URL` as secret (pg8000 dialect, internal URL) |
| Storage | **Render Disk** | Mounted at `/data`, 1 GB initial |
| Edge/TLS | Render-managed | Auto HTTPS on `*.onrender.com` and custom domains; HTTP→HTTPS redirect |
| Secrets | Render env (secret type) | All credentials entered ONLY in the Render dashboard |

Hard dependency (nothing invented, this is the app's config gate:
`validate_production_config` in `src/newsforge/config.py`): a `production` boot is
**refused** unless `NEWSFORGE_DATABASE_URL` (PostgreSQL), `NEWSFORGE_ADMIN_TOKEN`,
`NEWSFORGE_MOCK_AI=false` with an explicit real provider, and an **https**
`NEWSFORGE_SITE_URL` are all present.

## 2. Web Service spec

| Setting | Value | Source of truth |
|---------|-------|-----------------|
| Build method | Docker image built from the repo `Dockerfile` (final stage = `runtime`) | Dockerfile |
| Runtime user | `newsforge` (non-root, uid/gid **10001**) | Dockerfile L79 |
| Workdir | `/app` | Dockerfile L53 |
| Bind | `0.0.0.0` (image `ENV NEWSFORGE_HOST=0.0.0.0`; uvicorn `--host 0.0.0.0`) | Dockerfile L42/L82 |
| Port | Render `PORT` (default **10000**) | Render docs; image default 8000 — see Start Command |
| Start command (override) | `uvicorn newsforge.web.app:app --host 0.0.0.0 --port "$PORT"` | infra choice, applied by owner in dashboard |
| Health check path | `/health` | Web Service settings field |
| Readiness | `/ready` (DB connected + migration at head + schema OK) | app endpoint; used by smoke |
| Production env | `NEWSFORGE_ENVIRONMENT=production` (§4) | app config gate |
| Restart behavior | Render default (restart instances on failure, rolling deploys; paid instance = always-on, no spin-down) | Render docs |
| Instance | **Starter** (512 MB / 0.5 CPU) minimum | §12 cost |

Notes:
- Image `CMD` binds `0.0.0.0:8000`; Render expects the service to bind the value of
  `PORT` (default `10000`). Deterministic approach: **override the Start Command**
  to bind `$PORT` (Render expands env vars in the command). Equivalent fallback
  documented in §4 (`NEWSFORGE_PORT`).
- Free tier is **excluded** (spin-down after ~15 min idle + cold starts).
- No `.env` file exists in the image (verified in CI); config is env-only.

## 3. PostgreSQL spec

| Item | Value |
|------|-------|
| Product | Render Postgres (paid, managed) |
| Major version | **16.x** — matches the PostgreSQL version verified by the CI suite (`test_pg_compat.py`, PostgreSQL 16.2) |
| Database name | owner-chosen at creation (e.g. `newsforge`) |
| Region | Europe (same region as the Web Service) |
| Connection | **internal URL** (same account + region → Render private network; external URL traverses the public internet and must NOT be used by the app) |
| `NEWSFORGE_DATABASE_URL` (secret) | `postgresql+pg8000://USER:PASSWORD@INTERNAL_HOST:PORT/DB` — the dashboard's `postgresql://` URL is rewritten to the **pg8000** dialect because the image does not install psycopg2 (verified in CI). |
| Transport security | Render Postgres data encrypted at rest (AES-256); external connections use Render-managed TLS. Internal connections run over the private network; if Render enforces TLS there too, add `ssl_context` in `build_engine` — tracked as a potential infra tweak, **not invented now** (verify at first connectivity test). |
| Migration command | Automatic at boot: `init_production_db()` runs Alembic `upgrade head` **before** serving (connect → `SELECT 1` → migrate → migration gate → serve). Manual/controlled equivalent: `PYTHONPATH=/app/src alembic -c src/newsforge/db/migrations/alembic.ini upgrade head` with `NEWSFORGE_DATABASE_URL` set. |

Never write credentials anywhere in the repo (scan enforced by
`tests/test_ops_security.py`); `DATABASE_URL` is a **secret env var only**.

## 4. Secrets spec

### PUBLIC_CONFIG (non-secret; can go in Render env as plain values)

| Variable | Value (production intent) | Gate |
|----------|---------------------------|------|
| `NEWSFORGE_ENVIRONMENT` | `production` | enables all fail-fast gates |
| `NEWSFORGE_MOCK_AI` | `false` | MOCK refused in production |
| `NEWSFORGE_DEFAULT_PROVIDER` | `openai` (or `lm_studio`/`ollama`) | explicit real provider |
| `NEWSFORGE_MEDIUM_MODEL` | non-empty (e.g. `gpt-4o`) | required for openai/lm_studio |
| `NEWSFORGE_SMALL_MODEL` / `NEWSFORGE_LARGE_MODEL` | non-empty | optional overrides |
| `NEWSFORGE_AI_TIMEOUT_S` | `30` | must be > 0 |
| `NEWSFORGE_AI_MAX_TOKENS` | `500` | must be > 0 |
| `NEWSFORGE_SITE_URL` | `https://<decided>` | **hard https gate; see §5** |
| `NEWSFORGE_HOST` | `0.0.0.0` | Render bind requirement |
| `NEWSFORGE_PORT` | `10000` (or rely on Start Command `$PORT`) | infra choice |
| `NEWSFORGE_DEBUG` | `false` | debug refused in production |
| `NEWSFORGE_BACKUP_DIR` | `/data/backups` | image default |
| `NEWSFORGE_OPENAI_BASE_URL` | default `https://api.openai.com/v1` | optional override only if verified |
| `NEWSFORGE_DB_PATH` | image default `/data/newsforge.db` | SQLite fallback, never used by prod path |

### SECRET_CONFIG (Render env, secret type — masked in dashboard)

| Variable | Notes |
|----------|-------|
| `NEWSFORGE_DATABASE_URL` | internal URL rewritten to `postgresql+pg8000://…` (§3) |
| `NEWSFORGE_ADMIN_TOKEN` | owner-generated strong random; gates `/analytics`, `/metrics` |
| `NEWSFORGE_OPENAI_API_KEY` | only when provider is `openai` |
| `NEWSFORGE_LM_STUDIO_API_KEY` | only when provider is `lm_studio` |

Rules: no real values in the chat, the repo, `.env.example` (untouched), image
layers, logs, metrics, or CI artifacts — all enforced by the production-security
suites. `missing_production_secrets()` lists exactly the above as the required set.

## 5. Site URL — domain handling

- `DOMAIN=NOT_SELECTED`. Nothing is invented or configured in DNS.
- The architecture accepts any https `NEWSFORGE_SITE_URL`; the first live test may
  use Render's **automatic `https://<service>.onrender.com`** URL once the Web
  Service exists (Render-provisioned TLS, no domain needed).
- The owner then swaps to a custom domain: add the domain + DNS in Render, and set
  `NEWSFORGE_SITE_URL=https://<custom-domain>`. HTTP→HTTPS redirect is provided by
  Render's auto-redirect toggle.
- Until an https SITE_URL is set, running with `NEWSFORGE_ENVIRONMENT=production`
  is **blocked by design**; the provisional onboarding deploy may run
  `NEWSFORGE_ENVIRONMENT=staging` if desired (owner decision).

## 6. Database migration procedure (for the first real deploy)

```text
Postgres provisioned        [OWNER]
        ↓
DATABASE_URL configured     [OWNER] secret env, internal URL, pg8000 prefix
        ↓
Alembic upgrade head        app boot: init_production_db() runs migrate-db (never create_all)
        ↓
production config gate      assert_production_safe(); refusals are fail-fast (§10)
        ↓
service startup
        ↓
/live  /ready  /health      verification endpoints
        ↓
smoke tests                 scripts/deploy_smoke.py + §11 web-first plan
```

No migration is executed against a DB that does not exist (nothing was
provisioned; the DB write path is Alembic-only, non-destructive by policy, and
refuses an incompatible on-disk revision).

## 7. Persistent disk spec

| Item | Value |
|------|-------|
| Product | Render Disk |
| Mount | `/data` (Web Service disk mount) |
| Initial size | **1 GB** (SSD `$0.25/GB/mo`, see §12) |
| Persist | anything under `/data` that a container writes: `NEWSFORGE_BACKUP_DIR=/data/backups` (dumps), artifacts; DB file fallback `NEWSFORGE_DB_PATH=/data/newsforge.db` (never in prod) |
| Do NOT store here | MySQL/PG data (lives in Render Postgres), secrets, application code, logs intended for ephemeral processing |
| Relation to backups | `/data/backups` holds local `pg_dump` custom-format archives; off-site copies must be pulled by the owner (see §8) |

Not provisioned in this phase.

## 8. Backups spec (policy ≠ restore drill)

**Backup policy**
1. Render Postgres built-in continuous backups with **point-in-time recovery**:
   window 3 days (Hobby workspace) / 7 days (Pro workspace).
2. On-demand logical exports via the dashboard (`pg_dump`, retained 7 days by Render).
3. Our `PostgresBackupProvider` nightly `pg_dump` custom format → `/data/backups` on the Disk; owner periodically pulls off-site.

**Restore drill — kept strictly separate**
- `BACKUP_TESTED=false` and `RESTORE_DRILL=UNEXECUTED` until a restore is executed
  against the real infrastructure (validate `/ready` + data spot-checks on a
  scratch instance) **before** `PRODUCTION_OPERATIONAL=true` is declared.
- Render warning (from Render docs): backups are **deleted when the database is
  deleted** → external copies are mandatory.

## 9. Rollback spec

- Deploys are immutable images pinned to a commit (no `latest`); image identity =
  commit SHA + digest + version (CI-verified).
- Rollback = **deploy the previous release** from the Web Service deploy history
  (Render re-runs the prior image, rolling replacement).
- Migration compatibility: the migration gate makes an old app against a newer
  database **fail visibly** instead of corrupting data.
  - Before a migration ran: rollback = redeploy previous image (safe).
  - After a migration ran: do NOT point the old app at the migrated DB; restore
    from backup or deploy a matching app.
- `ROLLBACK_DRILL=UNEXECUTED` until a real deployment exists; the drill runs
  inside the controlled rollout window, never against live production data unless
  coordinated.

## 10. Production config refusals (verified by tests, no contract changes)

`tests/test_production_security.py` (38 passed, offline) verifies production
startup refuses each of:

- SQLite engine/database URL (PostgreSQL only);
- `NEWSFORGE_MOCK_AI=true` (config gate + `AiRouter` guard);
- missing `NEWSFORGE_DATABASE_URL`;
- missing `NEWSFORGE_ADMIN_TOKEN`;
- missing/invalid AI provider or OpenAI key, non-positive timeout/max tokens;
- invalid migration state (on-disk revision ≠ build head);
- non-https `NEWSFORGE_SITE_URL`;
- `NEWSFORGE_DEBUG=true`.

## 11. Web-first smoke test plan (immediately after the first deploy)

Ordered against Render's https URL once the Service is live:

1. open the Render HTTPS URL (provisional `*.onrender.com` or custom domain);
2. homepage `/`;
3. `/articles`;
4. open one article page;
5. `/sitemap.xml`;
6. `/feed.xml`;
7. `/live`;
8. `/health`;
9. `/ready`;
10. `/metrics` with `NEWSFORGE_ADMIN_TOKEN` auth (anonymous → 403).

Then exercise the pipeline end-to-end:
`source → story → decision → AI → publish → article visible`.

Automation: `scripts/deploy_smoke.py` runs the public + authed endpoint checks;
the URL/auth come from env (`NEWSFORGE_SMOKE_BASE_URL`, `NEWSFORGE_ADMIN_TOKEN`).
Also verify: `X-Request-ID` on responses, no secrets in responses/log surface,
`/ready` reports DB connected + version/commit expected for the deployed image.

## 12. Cost (estimate only — confirm in the Render dashboard before creating anything)

| Item | Plan | Est. monthly |
|------|------|--------------|
| Web Service | Starter (512 MB / 0.5 CPU) | $7 |
| Render Postgres | smallest paid (e.g. Basic-256 MB; price shown in dashboard) | ~$6–7 |
| Render Disk | 1 GB @ $0.25/GB | ~$0.25 |
| **Floor total (Hobby workspace)** | | **~$13–14/mo** |
| Optional | Pro workspace (+$25/mo) extends PITR to 7 days | discretionary |
| Variable | bandwidth/build minutes beyond included tier | verify in dashboard |

Free tier is excluded for production (spin-down + cold starts). **No resource is
provisioned in this phase; final prices must be confirmed in the Render dashboard.**

## 13. Owner-manual steps (the next phase, after this commit)

1. Open/own the Render account, add billing, authorize spend.
2. Confirm Europe region label and current Postgres price in the dashboard.
3. Create **Render Postgres** 16.x (Europe); copy internal URL → rewrite to `postgresql+pg8000://…`.
4. Create the **Web Service** from the repo (Docker runtime, Starter+, Disk `/data` 1 GB, Health Check Path `/health`, Start Command override §2).
5. Enter all env vars (§4) — secrets only via Render's secret env type.
6. Use the provisional `*.onrender.com` HTTPS URL as `NEWSFORGE_SITE_URL`, or decide the real domain (DNS/TLS via Render) first.
7. Trigger the first deploy; watch `/ready`; run §11 smoke plan.
8. Execute `RESTORE_DRILL` (+ later `ROLLBACK_DRILL`); schedule logical exports.
9. Approve production (`PRODUCTION_DEPLOYMENT=SUCCESS`).

## Files of record

- This file: `deployment/RENDER_ARCHITECTURE.md`
- Operational contract: `PRODUCTION_READINESS.md` · quick reference: `DEPLOYMENT.md`
- Smoke script: `scripts/deploy_smoke.py`