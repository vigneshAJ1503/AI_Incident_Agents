# Company profile template

The starting point for a new company. Don't edit it in place; create your own copy:

```bash
cd backend
uv run aiops profile init acme --company "Acme Corp"   # copies this folder to profiles/acme
$EDITOR ../profiles/acme/profile.yaml ../profiles/acme/services.yaml
cp ../profiles/acme/.env.example ../profiles/acme/.env    # fill in secrets (gitignored)
uv run aiops profile validate acme
export AIOPS_PROFILE=acme
```

The full guide is [docs/portability.md](../../docs/portability.md): provider matrix, which
settings to change, read-only credentials, secrets, and the day-1 checklist.

## Files

| File | What | Required |
|------|------|----------|
| `profile.yaml` | LLM, capabilities (provider + MCP + allowlist + settings), limits, guardrails, agents, `metadata` | yes |
| `services.yaml` | Service catalog: aliases, owners, dependencies, runbooks, identifiers per capability and environment | yes (or inherited through `extends`) |
| `.env.example` | Every `${VAR}` the profile uses, with comments. Copy it to `.env` (gitignored). | yes |
| `prompts/<agent>/vN.md` | Prompt overrides. They replace `config/prompts/<agent>/vN.md` of the same version, or add a newer version. | no |

## Providers at a glance

| Capability | Implemented (config only) | Planned (PR-P2..P4) |
|------------|---------------------------|---------------------|
| logs | `elasticsearch` (incl. Elastic Cloud), `loki` (incl. Grafana Cloud Logs; see `profiles/local-loki`) | `opensearch`, `splunk`, `datadog` |
| metrics | `prometheus` (any Prometheus-compatible API: Thanos, Mimir, VictoriaMetrics, AMP, GMP) | `datadog` |
| alerts | `alertmanager` | `pagerduty`, `opsgenie` |
| k8s | `kubernetes` (EKS, GKE, AKS, ...) | – |
| code | `git` (read-only clones) | `github`, `gitlab` |
| tickets | `jira` (mcp-atlassian contract), `mock` | – |
| knowledge | `postgres_fts` (markdown runbooks) | `confluence` |
| LLM | `openai_compat` (Groq, Gemini, OpenAI, Azure OpenAI, LiteLLM) | `anthropic`, `bedrock` |

`aiops profile validate` reports a planned provider as an error, and names any setting a
provider needs, e.g. `capability logs: provider 'loki' needs setting 'stream_labels'`.

## Keeping it private

This repository is public. `profiles/*` is gitignored except the shipped `local`,
`local-k8s` and `_template`. Keep a company profile in a private repository and point
`AIOPS_PROFILES_DIR` at its parent folder:

```bash
export AIOPS_PROFILES_DIR=~/work/acme-aiops-profiles   # contains acme/profile.yaml, ...
export AIOPS_PROFILE=acme
```
