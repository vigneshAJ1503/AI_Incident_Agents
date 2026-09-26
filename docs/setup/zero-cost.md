# Zero-Cost Development Approach

Goal: build and run the entire platform **for free** on a 16 GB Apple-silicon Mac.

> **Rule: no AI model ever runs on the local machine.** All LLM inference uses hosted free tiers. Tests and CI use a fake LLM. Knowledge search uses Postgres full-text search, which involves no model.

## Cost map

| Need | Free choice | Notes |
|------|-------------|-------|
| **LLM (primary)** | **Groq free tier** via the `openai_compat` provider (`https://api.groq.com/openai/v1`) | No credit card. Fast; open-weight models with tool calling. Rate limits apply (tokens/requests per minute), so agents are designed to be lean |
| **LLM (backup)** | **Google Gemini API free tier** via its OpenAI-compatible endpoint (`https://generativelanguage.googleapis.com/v1beta/openai/`) | Switch with 2 lines in `.env` if Groq limits are hit |
| **LLM in tests/CI** | `FakeLLMProvider` + recorded MCP fixtures | **Zero tokens.** Every unit test and all CI run without any LLM |
| Containers | Docker Desktop (free for personal use), or Colima (open source, lighter) | Give Docker 8–10 GB |
| Kubernetes | Minikube | Free |
| Logs / metrics / alerts | Elasticsearch & Kibana (basic license), Prometheus, Grafana OSS, Alertmanager | Free |
| MCP servers | Our own FastMCP servers + open-source community servers | Free |
| Knowledge search | **Postgres full-text search** (`tsvector` + ranking), no model | Optional later: hosted free-tier embeddings (e.g. Gemini) + pgvector, switchable in config |
| Database / cache | Postgres + pgvector, Redis | Free |
| Tickets | **Jira Cloud Free** (≤ 10 users) **+** our `mock-tickets-mcp` | CI and offline use run against the mock; see [jira.md](jira.md) |
| Git hosting / CI | GitHub public repo + GitHub Actions | Unlimited minutes for public repos |
| Code agent | Local Git repository via Git MCP | Free |

**The Anthropic/Claude provider stays in the design** as an optional provider for companies that have a key (added in PR-043 with the other enterprise providers). It is not needed to build or demo anything.

## How we stay within free LLM limits
1. **Deterministic code does the heavy lifting.** Queries, aggregation, pattern clustering, baselines and anomaly math are plain Python. The LLM only plans follow-ups and writes the reasoning.
2. **Small prompts:** tool outputs are truncated and summarized before they reach the LLM.
3. **Bounded loops:** `max_tool_calls`, `max_steps` and token budgets for each agent.
4. **Model routing by role:** a small, fast model for extraction and classification; the strongest free model only for RCA.
5. **Record once, replay forever:** `--record` saves real tool responses as fixtures for tests.
6. **Retry with backoff** on HTTP 429 (rate limit); automatic fallback to a secondary provider is a later option.

## Memory budget: 5 GB for Docker

The whole platform runs inside **5 GB** of Docker memory. Optional UIs are off by default.

| Component | Memory | Notes |
|-----------|--------|-------|
| Elasticsearch (512 MB heap) | ~0.9 GB | `ES_HEAP`/`ES_MEM_LIMIT` in `.env` |
| Postgres + Redis + Alertmanager | ~0.1 GB | |
| MCP servers (6) | ~0.35 GB | kubernetes-mcp (PR-018): ~53 MiB, `mem_limit: 128m` |
| Minikube: 4 sample services, Fluent Bit, kube-state-metrics (from PR-015) | ~2.2 GB | `minikube start --memory=2200 --cpus=2`; Fluent Bit measured at ~9 MB (PR-016, [logging.md](logging.md)) |
| Prometheus (from PR-020) | ~0.07 GB (measured) | 30 s scrapes, 2-day retention, `mem_limit: 256m` |
| **Always-on total** | **~3.7 GB** | measured: 1.4 GB before Minikube |
| Kibana (`make ui-up`) | +0.6 GB | optional, on demand |
| Grafana (`make ui-up` / `make grafana-up`, from PR-020) | +0.2 GB (measured) | optional, on demand |

Agents, tests and evals never need the UIs. Use `make ui-up` to look at logs and dashboards, and `make ui-down` when you're done.

## One-time setup
```bash
# 1. Groq key (free): https://console.groq.com → API Keys
# 2. .env
cp .env.example .env
#   LLM_PROVIDER=openai_compat
#   OPENAI_COMPAT_BASE_URL=https://api.groq.com/openai/v1
#   OPENAI_COMPAT_API_KEY=<your key>
# 3. Verify
cd backend && uv run aiops llm ping
```
