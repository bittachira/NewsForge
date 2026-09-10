# NewsForge Production Dockerfile
# Minimal production-ready image for NewsForge MVP

# Use Python 3.12 slim (compatible with requirements.txt)
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NEWSFORGE_MOCK_AI=true \
    NEWSFORGE_HOST=0.0.0.0 \
    NEWSFORGE_PORT=8000

# Set working directory
WORKDIR /app

# Install system dependencies (for production)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code (exclude tests, git metadata)
COPY src/ ./src/
COPY .env.example .env.template

# Create data directory for SQLite database (must be outside container filesystem in production)
RUN mkdir -p /app/data && \
    touch /app/data/.gitkeep  # Placeholder to keep volume mounted

# Set environment from template (production will override with actual .env)
COPY .env.template .env
ENV NEWSFORGE_DB_PATH=/data/newsforge.db

# Run the application with Uvicorn
CMD ["uvicorn", "src.newsforge.web.app:app", "--host", "0.0.0.0", "--port", "8000"]