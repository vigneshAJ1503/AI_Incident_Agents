# ADR-0002: Modular monolith with a custom, lightweight orchestrator

- **Status:** Accepted
- **Date:** 2026-09-25

## Context
Two options were considered:
- **One deployable service per agent** (`services/<agent>`), from the draft plan.
- **A single Python package** with agent modules.

We also considered agent frameworks (LangGraph, CrewAI, Claude Agent SDK) against a small custom orchestrator.

## Decision
- **Modular monolith:** one `aiops` package with `agents/<name>/` modules. External systems are reached only via MCP servers, which run as separate containers.
- **Custom orchestrator:** planner → asyncio DAG executor (up to 2 rounds) → RCA agent → response builder, built on typed contracts (`AgentTask` → `AgentResult`).
- **LLM behind a `LLMProvider` interface** (`anthropic`, `openai_compat`).

## Consequences
- Much less Docker/CI overhead locally; each agent is still independently testable via the CLI.
- The typed contracts keep a later split into separate services (A2A, backlog) cheap.
- There's no framework to lean on, so retries, timeouts and budgets are implemented and tested by us.
