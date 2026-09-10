# NewsForge runtime image (OPS_HARDENING_REPRODUCIBILITY).
#
# Deterministic build inputs:
#   * BASE is pinned to a specific python:3.12.14-slim DIGEST -> identical base
#     layers every build, regardless of when it runs.
#   * requirements.txt is fully ==-pinned (incl. transitive closure) -> the exact
#     same Python dependency set every build.
#   * Build tools (gcc, libc6-dev) exist ONLY in the builder stage; the runtime
#     image ships no compiler at all (deps come pre-built from the venv).
#   * SOURCE_IDENTITY (GIT_COMMIT / VERSION / BUILD_TIME) is injected as build ARG
#     and surfaced as NEWSFORGE_* env vars. Defaults are deterministic ("unknown");
#     a SHA or timestamp is never invented. CI passes BUILD_TIME from the commit
#     timestamp, so the metadata stays a pure function of the source commit.
#   * No .env file is baked into the image; configuration comes from env only.

ARG PYTHON_BASE_IMAGE=python:3.12.14-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# ---------------------------------------------------------------------------
# builder - resolves and installs the pinned runtime dependency set once.
# ---------------------------------------------------------------------------
FROM ${PYTHON_BASE_IMAGE} AS builder
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.txt

# ---------------------------------------------------------------------------
# runtime - lean, non-root, no build tools; exactly the app + pinned deps.
# ---------------------------------------------------------------------------
FROM ${PYTHON_BASE_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
    NEWSFORGE_MOCK_AI=true \
    NEWSFORGE_HOST=0.0.0.0 \
    NEWSFORGE_PORT=8000

# Source identity surfaced at runtime (deterministic per commit; defaults unknown).
ARG GIT_COMMIT=unknown
ARG VERSION=unknown
ARG BUILD_TIME=unknown
ENV NEWSFORGE_GIT_COMMIT=${GIT_COMMIT} \
    NEWSFORGE_VERSION=${VERSION} \
    NEWSFORGE_BUILD_TIME=${BUILD_TIME}

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY requirements.txt /app/requirements.txt
COPY src/ /app/src/

# Non-root runtime user (OPS security: the web/db process must NEVER run as root).
# /app stays owned+writable by the runtime user: CI runs the in-container pytest
# suite with cwd=/app (test temp files under .pytest_tmp). /data (DB + backups)
# MUST remain writable by the runtime user; both /app and /data can be remounted
# read-only / volumes by an administrator who does not run in-container tests.
RUN groupadd --system --gid 10001 newsforge \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
        --shell /usr/sbin/nologin newsforge \
    && mkdir -p /data/backups \
    && touch /data/.gitkeep \
    && touch /data/backups/.gitkeep \
    && chown -R newsforge:newsforge /app /data

ENV NEWSFORGE_DB_PATH=/data/newsforge.db \
    NEWSFORGE_BACKUP_DIR=/data/backups

LABEL org.opencontainers.image.source="https://github.com/bittachira/NewsForge"
LABEL org.opencontainers.image.revision=${GIT_COMMIT}
LABEL org.opencontainers.image.version=${VERSION}

USER newsforge

EXPOSE 8000
CMD ["uvicorn", "newsforge.web.app:app", "--host", "0.0.0.0", "--port", "8000"]

# ---------------------------------------------------------------------------
# test - runtime + pytest overlay. Used ONLY by CI to run the suite in-container.
# Ships test/dev tools that the runtime image deliberately does not include.
# ---------------------------------------------------------------------------
FROM runtime AS test
USER root
COPY requirements-dev.txt /app/requirements-dev.txt
RUN /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check -r /app/requirements-dev.txt
USER newsforge