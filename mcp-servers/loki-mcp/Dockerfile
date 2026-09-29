# Read-only Loki MCP server. Multi-stage, non-root, locked dependencies.
FROM ghcr.io/astral-sh/uv:0.11.21 AS uv

FROM python:3.12.13-slim-trixie AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12.13-slim-trixie
ARG VERSION=0.1.0
ARG REVISION=unknown
LABEL org.opencontainers.image.title="loki-mcp" \
      org.opencontainers.image.description="AI Incident Agents: read-only Loki MCP server (logs capability)" \
      org.opencontainers.image.source="https://github.com/vigneshAJ1503/AI_Incident_Agents" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
# Security (PR-042/047, Trivy): the pinned base + today's Debian security fixes, a numeric
# UID, then no shell, perl or package manager at runtime (the dpkg database stays for
# scanners). Runs with a read-only root filesystem (writable: /tmp). Last RUN.
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 mcp \
    && rm -f /usr/bin/apt* /usr/bin/dpkg* /usr/sbin/dpkg* /usr/bin/perl* \
       /usr/bin/bash /usr/bin/rbash /usr/bin/dash /usr/bin/sh /bin/bash /bin/dash /bin/sh
COPY --from=build --chown=10001:10001 /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8110
# Exec form: there is no shell.
HEALTHCHECK --interval=10s --timeout=5s --retries=10 \
  CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8110), 2)"]
ENTRYPOINT ["loki-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8110"]
