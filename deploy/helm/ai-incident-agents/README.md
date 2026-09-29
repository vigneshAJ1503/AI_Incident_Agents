# ai-incident-agents Helm chart

Installs AI Incident Agents (API, Web UI, Postgres evidence store, read-only MCP servers) with
**configuration only**: a company profile + service catalog, and credentials from Secrets you
already have. Full walkthrough: [docs/setup/helm.md](../../../docs/setup/helm.md).

## Quick install

```bash
kubectl create namespace aiops
# The bundled Postgres password (a Secret you own; the chart never holds secret values).
kubectl -n aiops create secret generic aiops-postgres --from-literal=password="$(openssl rand -hex 24)"
# Optional: a hosted LLM key. Without it, investigations are zero-token replays.
kubectl -n aiops create secret generic aiops-llm --from-literal=OPENAI_COMPAT_API_KEY=...
# Named API keys (approver identities) + the key the web proxy uses.
kubectl -n aiops create secret generic aiops-api-auth \
  --from-literal=AIOPS_API_KEYS="ui:$(openssl rand -hex 24)" ...

helm upgrade --install aiops deploy/helm/ai-incident-agents -n aiops -f my-values.yaml
helm test aiops -n aiops
kubectl -n aiops port-forward svc/aiops-ai-incident-agents-web 3100:3100
```

Try it on Minikube: `make helm-install-minikube` (values: `deploy/helm/values-minikube.yaml`).

## What it deploys

| Component | Kind | Default | Notes |
|---|---|---|---|
| api | Deployment + Service | on | `aiops serve`; readiness `/api/health`, liveness TCP |
| web | Deployment + Service (+ Ingress) | on | the only entry point: proxies `/api/*` (REST + SSE) |
| postgresql | StatefulSet + PVC | on | single instance; or `externalDatabase.existingSecret` (URL) |
| migrate | Job (Helm hook) | on | `aiops db upgrade` post-install / pre-upgrade (+ optional demo seed) |
| mcp-mock-tickets | Deployment + Service | on | offline Jira stand-in (approved drafts become tickets) |
| mcp-{elasticsearch,loki,prometheus,alertmanager,kubernetes,git,knowledge} | Deployment + Service | off | one toggle each; the API gets `<CAP>_MCP_URL` |
| k8s-reader | ServiceAccount + read-only RBAC | with `mcpServers.kubernetes` | get/list/watch in `k8sReader.namespaces` only |
| NetworkPolicy, PodDisruptionBudget | | off | toggles |

Every pod: `runAsNonRoot` (UID 10001; Postgres 70), `readOnlyRootFilesystem`, all capabilities
dropped, `seccompProfile: RuntimeDefault`, no ServiceAccount token (except the k8s reader),
CPU/memory limits. `values.schema.json` rejects unknown keys, non-string env values and
`latest` tags; `make helm-lint` also checks those guardrails on the rendered manifests.

## Key values

| Key | Default | Meaning |
|---|---|---|
| `profile.name` | `helm` | `AIOPS_PROFILE` |
| `profile.create` / `profileYaml` / `servicesYaml` | `true` / `extends: local` + Postgres storage / `""` | the profile and catalog, as a ConfigMap mounted at `/app/profiles/<name>/` |
| `profile.existingConfigMap` | `""` | your own ConfigMap (keys `profile.yaml`, `services.yaml`) |
| `llm.existingSecret` | `""` | every key becomes an API env var (`OPENAI_COMPAT_API_KEY`, ...) |
| `llm.env` / `api.env` | `{}` | non-secret settings the profile reads as `${VAR}` |
| `api.auth.existingSecret` | `""` | `AIOPS_API_KEYS` / `AIOPS_API_KEY` (empty = no auth; NOTES warns) |
| `web.apiKeySecret` | `{name: "", key: AIOPS_UI_API_KEY}` | server-side key of the web proxy |
| `postgresql.auth.existingSecret` | `aiops-postgres` | required when `postgresql.enabled` |
| `externalDatabase.existingSecret` / `urlKey` | `""` / `url` | `postgresql://USER:PASS@HOST:5432/DB` |
| `migrations.useHelmHooks` / `seedDemo` | `true` / `false` | plain Job per revision for GitOps; demo history for trials |
| `mcpServers.<name>.enabled` / `env` / `existingSecret` | see values.yaml | the server's guardrails and credentials |
| `networkPolicy.enabled` / `podDisruptionBudget.enabled` / `ingress.enabled` | `false` | |

See `values.yaml` for every key (documented inline).
