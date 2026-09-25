# ADR-0001: Python + FastAPI backend, Next.js frontend

- **Status:** Accepted
- **Date:** 2026-09-25

## Context
The platform is agent- and MCP-heavy. We need fast iteration, a strong AI and MCP ecosystem, typed schemas for structured LLM output, and a production-grade UI.

## Decision
- Backend: Python 3.12, managed with `uv`. FastAPI for the API, Pydantic v2 for schemas and config, asyncio for concurrent agents.
- Frontend: Next.js (App Router) + TypeScript + Tailwind + shadcn/ui + Recharts.
- Quality: ruff, mypy (strict), pytest, pre-commit, GitHub Actions.

## Consequences
- One language for agents, MCP servers (FastMCP) and the API.
- Strict typing from day one; slightly more upfront effort.
