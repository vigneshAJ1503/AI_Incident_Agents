# Zero-Cost Development Approach

Goal: build and run the entire platform **for free** on a 16 GB Apple-silicon Mac.

## Cost map

| Need | Free choice | Notes |
|------|-------------|-------|
| **LLM (primary)** | **Groq free tier** via the `openai_compat` provider (`https://api.groq.com/openai/v1`) | No credit card. Fast; open-weight models with tool calling. Rate limits apply (tokens/requests per minute), so agents are designed to be lean |
| **LLM (backup)** | **Google Gemini API free tier** via its OpenAI-compatible endpoint (`https://generativelanguage.googleapis.com/v1beta/openai/`) | Switch with 2 lines in `.env` if Groq limits are hit |
| **LLM (offline)** | **Ollama** (`http://localhost:11434/v1`) with a 7–8B tool-calling model | Only when the Docker stack is **not** running. 16 GB RAM can't hold ES + Minikube + a local model at once |
| **LLM in tests/CI** | `FakeLLMProvider` + recorded MCP fixtures | **Zero tokens.** Every unit test and all CI run without any LLM |
| Containers | Docker Desktop (free for personal use), or Colima (open source, lighter) | Give Docker 8–10 GB |
| Kubernetes | Minikube | Free |
| Logs / metrics / alerts | Elasticsearch & Kibana (basic license), Prometheus, Grafana OSS, Alertmanager | Free |
| MCP servers | Our own FastMCP servers + open-source community servers | Free |
| Embeddings | `fastembed` (local ONNX, small model) | Free; no API |
| Database / cache | Postgres + pgvector, Redis | Free |
| Tickets | **Jira Cloud Free** (≤ 10 users) **+** our `mock-tickets-mcp` | CI and offline use run against the mock |
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

## Memory budget (16 GB Mac)

| Component | RAM |
|-----------|-----|
| Elasticsearch (1 GB heap) | ~1.5 GB |
| Kibana | ~0.8 GB |
| Postgres + Redis | ~0.3 GB |
| MCP servers + backend | ~0.5 GB |
| Minikube (from PR-015) | ~3 GB |
| Prometheus + Grafana + Alertmanager (from PR-020) | ~0.8 GB |

Bring up only the slice you're working on (`make infra-up` profiles). Stop Kibana when you don't need the UI.

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
