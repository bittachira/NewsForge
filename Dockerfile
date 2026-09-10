# NewsForge Production Dockerfile
# Minimal production-ready image for NewsForge MVP

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NEWSFORGE_MOCK_AI=true \
    NEWSFORGE_HOST=0.0.0.0 \
    NEWSFORGE_PORT=8000

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user (OPS security: the web/db process must NEVER run as root).
RUN groupadd --system --gid 10001 newsforge \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
        --shell /usr/sbin/nologin newsforge

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Make the newsforge package importable (src layout; uvicorn console script does
# not add /app or /app/src to sys.path and the app imports top-level newsforge.*).
ENV PYTHONPATH=/app/src

# Create data directory for SQLite database (absolute path /data)
RUN mkdir -p /data/backups && \
    touch /data/.gitkeep && \
    touch /data/backups/.gitkeep

# Allow the runtime user to read the app and WRITE /data (DB init at startup).
RUN chown -R newsforge:newsforge /app /data

# Set environment from example template (production will override with actual .env)
COPY .env.example .env
ENV NEWSFORGE_DB_PATH=/data/newsforge.db
ENV NEWSFORGE_BACKUP_DIR=/data/backups

USER newsforge

CMD ["uvicorn", "newsforge.web.app:app", "--host", "0.0.0.0", "--port", "8000"]