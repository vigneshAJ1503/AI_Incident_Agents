# ADR-0012: Provider adapters (agents say what, providers say how)

- **Status:** Accepted
- **Date:** 2026-09-28

## Context
ADR-0011 made a company's setup pure configuration, but the PR-P1 audit
(docs/portability.md) found vendor logic still in agent code: the Log agent wrote ES|QL and
Kibana KQL and called `execute_esql` by name, the Tickets agent wrote JQL for the
mcp-atlassian tool contract, the Alert agent built Alertmanager matchers, the Code agent
named git-mcp tools, and the prompts taught those query languages. A company on Loki,
PagerDuty or GitHub would have had to edit agents.

## Decision
- **Per-capability interfaces** in `backend/src/aiops/providers/<capability>/__init__.py`:
  an abstract adapter class (e.g. `LogsProvider`) whose methods are the *questions* the agent
  asks ("volume by level for this scope in window W vs baseline B", "patterns", "first
  occurrences of a phrase"), each returning a `ToolRequest` (tool name + arguments), plus
  provider-neutral result shapes (e.g. `LogTable` with fixed column names) and neutral
  deep links. The agent keeps the *what*: scope from the catalog, deterministic analysis,
  evidence, signals and the LLM loop.
- **One module per provider** (`providers/logs/elasticsearch.py`, ...) that registers itself
  in `PROVIDER_REGISTRY` keyed by `(capability, provider)`. `aiops.providers` discovers
  them like agents; modules starting with `_` (the `_skeleton.py` template) are never
  imported. `BaseAgent.provider(cap, Interface)` returns the adapter that
  `capabilities.<cap>.provider` selects, built with `capabilities.<cap>.settings`.
- **The registry feeds the provider matrix.** A provider class declares `mcp`, `required`
  settings, `agent_tools` and a `note`; `core.profiles.PROVIDERS` adds every registered
  adapter as *implemented*, so `aiops profile validate` can't claim a provider works
  without code behind it. Planned rows stay a static table.
- **Prompt fragments.** Query-language guidance moves out of agent prompts into
  `config/prompts/providers/<capability>/<provider>/vN.md`, rendered with the agent's
  variables into `$provider_guidance`. Only prompts that reference `$provider_guidance`
  get fragments, so older prompt versions and profile overrides keep working. The run's
  prompt ref records both (`logs/v3@<sha>+providers/logs/elasticsearch/v1@<sha>`).
- **Zero behavior change is the acceptance test:** extracted adapters must reproduce the
  old tool calls byte for byte. Every recorded replay fixture (39 runs, 159 tool calls)
  is replayed before/after and compared on tool calls, arguments, evidence (summary,
  query, link, data), status, signals and findings; only the prompt text may change.

## Alternatives considered
- **Query DSL / intermediate representation** (one neutral query language compiled to
  ES|QL, LogQL, SPL). More general, but a large project with a long tail of semantics; the
  agents ask a handful of fixed questions, so a method per question is simpler and each
  provider can use its vendor's idioms.
- **Configuration-only query templates** (ES|QL strings in YAML). Escaping, optional
  clauses (shared-index filter, version field) and result normalization need code; YAML
  templates would move the coupling, not remove it.
- **One adapter per MCP server instead of per capability.** Capabilities are what agents
  bind to (ADR-0002/0011); a provider is the unit a company swaps.

## Consequences
- Adding Loki/OpenSearch/PagerDuty/GitHub = a provider module + a prompt fragment + profile
  settings; no agent change (docs/portability.md, "How to add a provider"; PR-P4 proves it
  with a second logs backend).
- Provider methods are coarse by design; a new agent question means adding a method to the
  interface and to every provider of that capability.
- The metrics capability (PR-022, in progress) will be adapted in a later wave; until
  then its matrix row stays static.
