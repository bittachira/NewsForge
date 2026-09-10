# NewsForge MVP - Staging Deployment Configuration

## Status: PREPARATION COMPLETE

This directory contains the configuration and instructions for deploying NewsForge MVP to a staging environment using Docker containers.

## Current State

- **Release Commit**: `3b9873d` (HEAD)
- **Deployment Infrastructure**: Complete and committed
- **Docker Availability**: NOT AVAILABLE in current environment
- **Container Verification**: Requires external staging environment or CI/CD pipeline

## Staging Environment Setup

### Requirements

- Docker Engine 20.10+
- Docker Compose 2.0+ (optional, for multi-container setup)
- At least 512MB RAM recommended
- Persistent storage volume for SQLite database

### Quick Start Commands

```bash
# Build the NewsForge staging image
docker build -t newsforge-mvp:staging .

# Run with persistent database storage
docker run -d --name newsforge-staging \
    --publish 8000:8000 \
    -v $(pwd)/data:/data \
    --env-file .env.staging \
    newsforge-mvp:staging

# Health check
curl http://localhost:8000/health

# View logs
docker logs newsforge-staging
```

### Environment Variables for Staging

Create `.env.staging` in the repository root:

```bash
NEWSFORGE_DB_PATH=/data/newsforge.db
NEWSFORGE_MOCK_AI=true
NEWSFORGE_DEFAULT_PROVIDER=mock
NEWSFORGE_SITE_URL=http://localhost:8000
NEWSFORGE_HOST=0.0.0.0
NEWSFORGE_PORT=8000
NEWSFORGE_BRAND_NAME=Lumen
NEWSFORGE_TAGLINE="Verified light on what matters."
```

**IMPORTANT**: Never commit `.env.staging` with real credentials to version control.

## Staging Endpoints to Verify

| Endpoint | Expected Status | Purpose |
|----------|-----------------|---------|
| `/health` | 200 OK / 503 on failure | Minimal public health check |
| `/articles` | 200 OK | List published articles |
| `/articles/{slug}` | 200/404 | Individual article page |
| `/sitemap.xml` | 200 OK | Sitemap for discovery |
| `/feed.xml` | 200 OK | RSS feed |
| `/analytics` | 200 (with token) / 403 (anonymous) | INTERNAL BI dashboard; requires `NEWSFORGE_ADMIN_TOKEN` |

**Security (OPS hardening)**: `/docs`, `/redoc` and `/openapi.json` are disabled (404).
`/analytics` is fail-closed: without `NEWSFORGE_ADMIN_TOKEN` set it returns 403 for every
caller. The container runs as a non-root user; `/data` is owned by that user.

## Database Configuration

- **Path**: `/data/newsforge.db` (inside container)
- **Persistence**: Mounted volume at `/data`
- **Initialization**: Automatic on first request
- **Migration**: None required (schema created via ORM)

### Volume Mount Example

```yaml
volumes:
  - ./staging-data:/data
    # Or in CI/CD:
    # - ${STAGING_DATA_PATH}:/data
```

## Mock AI Mode for Staging

By default, NewsForge runs in MOCK mode:

```bash
NEWSFORGE_MOCK_AI=true
```

This allows:
- No external API dependencies
- Deterministic generation for reproducible testing
- Cost tracking via `ai_jobs` table
- Full E2E workflow without LLM calls

## CI/CD Pipeline Integration Example

### GitHub Actions (example)

```yaml
name: NewsForge Staging Tests

on: [push, pull_request]

jobs:
  staging-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v2
      
      - name: Build NewsForge image
        run: |
          docker build \
            --tag newsforge-mvp:${{ github.sha }} \
            .
      
      - name: Run health check
        run: |
          docker run --rm newsforge-mvp:${{ github.sha }} \
            bash -c "uvicorn src.newsforge.web.app:app & \
            sleep 5 && curl -s http://localhost:8000/health"
      
      - name: Run full test suite
        run: |
          docker run --rm newsforge-mvp:${{ github.sha }} \
            pytest -q tests
```

## Verification Checklist for Staging

### Functional Tests
- [ ] Container builds without warnings
- [ ] Health endpoint returns 200 OK with DB connected
- [ ] Articles endpoint shows published content (if any)
- [ ] Sitemap and RSS feed generate valid XML
- [ ] Analytics dashboard loads with BI data
- [ ] All 190 regression tests pass in container
- [ ] Empty database initialization works
- [ ] MOCK AI generation registers jobs correctly
- [ ] Idempotency: duplicate generations create single artifact

### Security Checks
- [ ] No secrets in Dockerfile or image layers
- [ ] `.env` files not copied to production image
- [ ] Only necessary Python packages installed
- [ ] No development dependencies in production
- [ ] Git directory excluded from image

### Resource Usage
- [ ] Startup time < 30 seconds
- [ ] Memory usage reasonable (< 256MB idle)
- [ ] No memory leaks on prolonged operation
- [ ] Graceful shutdown on SIGTERM

### Error Handling
- [ ] Invalid article slug returns 404 cleanly
- [ ] Missing DB path handled gracefully
- [ ] API errors don't expose stack traces
- [ ] Health check works even when DB is initializing

## Deployment Artifacts

### Required Files (all committed in `3b9873d`)
- `Dockerfile` - Production-ready container definition
- `.dockerignore` - Clean image build exclusions
- `.env.example` - Environment variable template
- `DEPLOYMENT.md` - Comprehensive deployment guide
- `start.py` - Manual startup script

### Generated Artifacts (not committed)
- Compiled requirements in image layers
- Runtime Python cache (`__pycache__/`)
- Staging database (volume mount, not committed)

## Next Steps for Staging Deployment

1. **Set up staging environment** with Docker available
2. **Build and test** using the commands above
3. **Verify all endpoints** return expected responses
4. **Run full E2E workflow** in staging container
5. **Confirm idempotency** (no duplicate artifacts)
6. **Test restart recovery** (state persists across stops)
7. **Review logs** for any warnings or errors
8. **Document staging results** for production deployment

## Monitoring and Logging

### Container Logs

```bash
docker logs newsforge-staging
docker logs --tail 100 newsforge-staging
```

### Health Check Script

```bash
#!/bin/bash
until $(curl -s http://localhost:8000/health | grep -q '"status": "ok"'); do
  echo "Waiting for NewsForge..."
  sleep 2
done
echo "NewsForge is ready!"
```

## Troubleshooting

### Container Won't Start

Check Docker daemon status:
```bash
docker info
```

Check container logs for errors:
```bash
docker logs newsforge-staging
```

### Database Not Persisting

Verify volume mount exists:
```bash
docker inspect newsforge-staging --format '{{ .Mounts }}'
```

### Health Check Fails

- Ensure database path is writable
- Check `NEWSFORGE_DB_PATH` environment variable
- Verify disk space for SQLite file

## Security Notes for Staging

- Never use production secrets in staging `.env` files
- MOCK mode should remain enabled during development/staging
- Use separate database path for staging vs production
- Rotate any API keys if real providers are tested
- Keep container images updated when rebuilding

## Migration to Production

Once staging is verified and stable:

1. Update `NEWSFORGE_SITE_URL` to production domain
2. Disable MOCK mode with real AI provider configuration
3. Configure persistent volume with larger storage
4. Set up proper logging aggregation
5. Configure health check monitoring/alerting
6. Update firewall/ingress rules for port 8000
7. Document production-specific environment variables

## References

- Main MVP Documentation: `DEPLOYMENT.md`
- Environment Template: `.env.example`
- Dockerfile: `Dockerfile`
- Start Script: `start.py`

---

**Generated**: 2026-09-10  
**Commit**: 3b9873d46692110b0398c44dd1760f04050d94b7  
**Status**: Staging deployment preparation complete, awaiting Docker environment for verification