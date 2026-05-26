# syntax=docker/dockerfile:1.7
# Multi-stage build:
#   * builder — has compilers + dev headers, installs pip deps into /opt/venv
#   * runtime — copies /opt/venv across, no compilers → smaller final image
#
# Same image runs in:
#   * docker-compose dev (DEBUG=True → entrypoint starts runserver)
#   * production / Render (DEBUG=False → entrypoint runs collectstatic + gunicorn)

ARG PYTHON_VERSION=3.12-slim-bookworm

# ──────────────────────────────────────────────────────────────────────────────
# Stage 1 — builder: install Python deps into a venv
# ──────────────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build-time system deps (compilers + headers for psycopg / cryptography / lxml).
# Removed from the final image — only /opt/venv comes across.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libpq-dev \
        libxml2-dev \
        libxslt1-dev \
        libssl-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Isolated venv so layer caching is predictable and the runtime stage can
# copy a single directory across.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Copy requirements FIRST so this layer caches on dep changes only — code
# changes won't bust the (slow) pip install layer.
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt


# ──────────────────────────────────────────────────────────────────────────────
# Stage 2 — runtime: lean image with only the venv + runtime libs
# ──────────────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=agent.settings \
    PORT=8000

# Runtime system deps only (no compilers). libpq5 is required by psycopg
# binaries at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libxml2 \
        libxslt1.1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. Anything written at runtime (logs, media) is owned by `app`.
RUN groupadd --system app && \
    useradd --system --gid app --home-dir /app --create-home --shell /bin/bash app

# Bring the venv across from the builder stage.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Copy app code (filtered by .dockerignore) + entrypoint, fix ownership.
COPY --chown=app:app . .
RUN chmod +x /app/docker-entrypoint.sh

USER app

EXPOSE 8000

# Docker-level healthcheck. Mirrors the /healthz/ Django endpoint we added —
# returns 200 if both the process and the DB are reachable.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8000/api/healthz/ || exit 1

ENTRYPOINT ["bash", "/app/docker-entrypoint.sh"]
