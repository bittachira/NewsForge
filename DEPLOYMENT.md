# NewsForge - Deployment Documentation

This directory contains the minimal infrastructure required to deploy NewsForge MVP in production.

## Quick Start

### Local Development

```bash
python -m uvicorn src.newsforge.web.app:app --reload
```

Then visit: http://localhost:8000

### Health Check

```bash
curl http://localhost:8000/health
```

Expected response (after DB init):
```json
{"status": "ok", "db": "connected"}
```

### Production Deployment

1. **Clone or copy the repository**

2. **Configure environment** (copy `.env.example` to `.env`):

```bash
cp .env.example .env  # Copy template first
# Edit .env with your configuration
```

3. **(Optional) Mount persistent data directory**

For Docker:
```yaml
volumes:
  - ./data:/data      # Persist SQLite database between restarts
```

4. **Start the application:**

```bash
NEWSFORGE_MOCK_AI=true python -m uvicorn \
    src.newsforge.web.app:app \
    --host 0.0.0.0 \
    --port 8000
```

5. **Verify deployment:**

```bash
curl http://localhost:8000/health
curl http://localhost:8000/articles
curl http://localhost:8000/sitemap.xml
curl http://localhost:8000/feed.xml
```

### Docker Deployment

```bash
docker build -t newsforge .
docker run -p 8000:8000 \
    -v $(pwd)/data:/data \
    newsforge
```

Then verify:
```bash
curl http://localhost:8000/health
```

## Environment Variables

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `NEWSFORGE_DB_PATH` | `data/newsforge.db` | No | Path to SQLite database file |
| `NEWSFORGE_MOCK_AI` | `true` | No | Enable offline/mock mode (no external API) |
| `NEWSFORGE_DEFAULT_PROVIDER` | `mock` | No | AI provider: `mock`, `openai`, `ollama` |
| `NEWSFORGE_OPENAI_API_KEY` | - | If using OpenAI | OpenAI API key |
| `NEWSFORGE_SITE_URL` | `http://localhost:8000` | No | Public URL of the application |
| `NEWSFORGE_HOST` | `127.0.0.1` | No | Bind address for Uvicorn |
| `NEWSFORGE_PORT` | `8000` | No | Port for Uvicorn |

## Database Initialization

The database is created automatically on first run. To reset:

```bash
rm -rf data/newsforge.db
# Then restart the application
```

## Health Endpoint

The `/health` endpoint verifies:
- Application is running
- Database connection is working

Returns JSON with status code 200 when healthy.

## Offline Mode (MOCK AI)

By default, NewsForge runs in MOCK mode (`NEWSFORGE_MOCK_AI=true`). No external AI API is required. This is suitable for initial deployment and testing.

To use a real AI provider:
1. Set `NEWSFORGE_MOCK_AI=false`
2. Configure your preferred provider (OpenAI, Ollama, etc.)
3. Provide necessary API credentials

## Security Notes

- Never commit `.env` files to version control
- Use environment variables or secrets management for production
- The Dockerfile does not include any AI API keys by default

## Logs

Application logs are written to stdout/stderr. For persistent logging, redirect output:

```bash
python -m uvicorn ... > app.log 2>&1
```

Or use Uvicorn's built-in log handlers for structured JSON logging.

## Support

For issues or questions, refer to:
- Project README: `README.md`
- Source code documentation in docstrings
- Issue tracker at repository root