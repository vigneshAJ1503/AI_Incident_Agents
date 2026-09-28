# One image for all sample services (SERVICE_NAME selects which). Multi-stage, non-root.
FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

FROM python:3.14.7-slim-trixie AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14.7-slim-trixie
# Security (PR-042, Trivy): the pinned base image + today's Debian security fixes.
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 app
COPY --from=build --chown=10001:10001 /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
# Numeric UID so Kubernetes runAsNonRoot can verify it.
USER 10001
EXPOSE 8080
ENTRYPOINT ["sample-app"]
