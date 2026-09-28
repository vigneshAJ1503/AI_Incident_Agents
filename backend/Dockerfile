# The aiops API (PR-035): `aiops serve` in a container. Multi-stage, pinned, non-root.
# Build context = the REPO ROOT (it needs config/, profiles/, scenarios/ and the recorded
# replay fixtures), e.g. via deploy/compose/docker-compose.app.yml or:
#   docker build -f backend/Dockerfile -t aiops/backend:0.1.0 .
# backend/Dockerfile.dockerignore keeps the context to what's copied below.
FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

FROM python:3.14.7-slim-trixie AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app/backend
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY backend/src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14.7-slim-trixie
# Security (PR-042, Trivy): the pinned base image + today's Debian security fixes.
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
# Numeric UID/GID (Kubernetes runAsNonRoot needs a number).
RUN groupadd --gid 10001 aiops && useradd --create-home --uid 10001 --gid 10001 aiops
WORKDIR /app
COPY --from=build --chown=10001:10001 /app/backend/.venv /app/backend/.venv
# Profiles, prompts, scenarios and replay fixtures are read-only data for the API.
COPY config ./config
COPY profiles ./profiles
COPY scenarios ./scenarios
COPY backend/tests/fixtures ./backend/tests/fixtures
# .data/: the file-based approval store and audit log (profile `guardrails.*_path`).
RUN mkdir -p /app/.data && chown 10001:10001 /app/.data
ENV PATH="/app/backend/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AIOPS_CONFIG_DIR=/app/config \
    AIOPS_PROFILE=local
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=10 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)" || exit 1
ENTRYPOINT ["aiops", "serve", "--host", "0.0.0.0", "--port", "8000"]
