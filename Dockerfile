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

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Create data directories for SQLite database
RUN mkdir -p /app/data && \
    touch /app/data/.gitkeep && \
    mkdir -p /tmp/staging-data && \
    touch /tmp/staging-data/.gitkeep  # Placeholder to keep volume mounted

# Set environment from example template (production will override with actual .env)
COPY .env.example .env
ENV NEWSFORGE_DB_PATH=/data/newsforge.db

CMD ["uvicorn", "src.newsforge.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
