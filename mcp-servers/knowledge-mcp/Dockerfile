# Read-only knowledge (runbook search) MCP server. Multi-stage, non-root, locked dependencies.
FROM ghcr.io/astral-sh/uv:0.11.21 AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 mcp
COPY --from=build --chown=mcp:mcp /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER mcp
EXPOSE 8108
HEALTHCHECK --interval=10s --timeout=5s --retries=10 \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8108), 2)" || exit 1
ENTRYPOINT ["knowledge-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8108"]
