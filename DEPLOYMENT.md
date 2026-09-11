# NewsForge Deployment

This file is the operational quick reference. The full security / gate / backup /
rollback contract lives in **[PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md)**.

> **Current status:** staging fully verified; the production deployment target is
> `UNSELECTED`. Nothing below deploys to a real host yet.

## Environments

- `NEWSFORGE_ENVIRONMENT=development|test|staging` — never gated.
- `NEWSFORGE_ENVIRONMENT=production` — fail-fast configuration gates are enforced
  at startup (see PRODUCTION_READINESS.md §2/§3/§8). Production is refused when
  any of these is absent/misconfigured: PostgreSQL `NEWSFORGE_DATABASE_URL`,
  `NEWSFORGE_ADMIN_TOKEN`, `NEWSFORGE_MOCK_AI=false` + an explicit real AI
  provider + key, public https `NEWSFORGE_SITE_URL`, no debug.

## Local development

```bash
python -m uvicorn src.newsforge.web.app:app --reload
```

Visits http://localhost:8000. Development defaults to SQLite + MOCK AI — no
network, no secrets.

## Staging / container

```bash
# staged image with deterministic build identity
docker build --tag newsforge:staging --target runtime \
  --build-arg GIT_COMMIT=$(git rev-parse HEAD) \
  --build-arg VERSION=0.1.0 \
  --build-arg BUILD_TIME="$GIT_AUTHOR_DATE" .

docker run -d -p 8000:8000 \
  --name newsforge-staging \
  -e NEWSFORGE_ENVIRONMENT=staging \
  -e NEWSFORGE_MOCK_AI=true \
  -e NEWSFORGE_ADMIN_TOKEN=<strong-random-token> \
  -v newsforge-data:/data \
  newsforge:staging
```

Verify: `/health`, `/live`, `/ready`, `/articles`, `/sitemap.xml`, `/feed.xml`.
Internal `/analytics` and `/metrics` require the admin token (fail-closed).

## Production (when a target is selected)

1. Provide the environment (see `.env.production.example`): PostgreSQL DSN,
   admin token, real AI provider + key, public https site URL, `NEWSFORGE_
   ENVIRONMENT=production`.
2. Run the image with an explicit command/port, `/data` on a persistent volume,
   `/app` mounted read-only, HTTPS terminated in front.
3. Startup performs: configuration gate → connect PostgreSQL → Alembic
   `upgrade head` → migration gate → serve. The process refuses to start on any
   gate failure.
4. Post-deployment smoke: `NEWSFORGE_SMOKE_BASE_URL=… NEWSFORGE_ADMIN_TOKEN=…
   python scripts/deploy_smoke.py`.

## Migrations

- Bootstrap/legacy/upgrade flows are automatic **and explicit** (Alembic):
  see PRODUCTION_READINESS.md §5.
- The DB alive at an incompatible revision fails fast; it is never silently
  mutated.

## Backups

- Nightly `pg_dump` (custom format) verified with `pg_restore --list`
  (`PostgresBackupProvider`, implemented + tested). Operator schedules the
  nightly job and the retention/restore drill; see PRODUCTION_READINESS.md §7.

## Rollback

- Redeploy the previous commit's image; the migration gate + schema boundary make
  an old app against a newer database fail visibly rather than corrupt data.
  Details in PRODUCTION_READINESS.md §14.

## Environment variables

| Variable | Production | Purpose |
|----------|-----------|---------|
| `NEWSFORGE_ENVIRONMENT` | `production` | enables fail-fast gates |
| `NEWSFORGE_DATABASE_URL` | required (PG) | `postgresql+pg8000://…` |
| `NEWSFORGE_ADMIN_TOKEN` | required | `/analytics`, `/metrics` gate |
| `NEWSFORGE_MOCK_AI` | `false` | MOCK refused in production |
| `NEWSFORGE_DEFAULT_PROVIDER` | `openai\|lm_studio\|ollama` | explicit real provider |
| `NEWSFORGE_OPENAI_API_KEY` | if openai | provider credential |
| `NEWSFORGE_SITE_URL` | required (https) | canonical/public URLs |
| `NEWSFORGE_BACKUP_DIR` | default `/data/backups` | offline backups |
| `NEWSFORGE_HOST` / `NEWSFORGE_PORT` | `0.0.0.0` / `8000` | uvicorn bind |
| `NEWSFORGE_DEBUG` | `false` | debug refused in production |

Never commit real values; use the secret provider / environment (see
PRODUCTION_READINESS.md §3).

## Support

- Full contract: [PRODUCTION_READINESS.md](./PRODUCTION_READINESS.md)
- Persistence notes: [PERSISTENCE.md](./PERSISTENCE.md)
- Reproducibility notes: [REPRODUCIBILITY.md](./REPRODUCIBILITY.md)