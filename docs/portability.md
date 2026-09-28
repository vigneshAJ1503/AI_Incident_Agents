# Portability: onboarding a new company with configuration only

**Goal:** when this project moves to a new company, you change **configuration, never code**.
Everything company-specific lives in one folder, `profiles/<company>/`, chosen by one
variable, `AIOPS_PROFILE`, and created by one command, `aiops profile init <company>`
(ADR-0011, MASTER_PLAN §17).

```
profiles/
  local/        profile.yaml services.yaml .env.example   the zero-cost demo (synthetic data)
  local-k8s/    profile.yaml services.yaml .env.example   extends local: real Minikube logs
  _template/    profile.yaml services.yaml .env.example README.md   commented starting point
  <company>/    created by `aiops profile init <company>`  (gitignored, see "Secrets")
config/prompts/ shared prompts; profiles/<company>/prompts/<agent>/vN.md overrides them
```

## Quick start

```bash
cd backend
uv run aiops profile init acme --company "Acme Corp"      # copies profiles/_template
$EDITOR ../profiles/acme/profile.yaml                     # providers, URLs, field mappings
$EDITOR ../profiles/acme/services.yaml                    # your services
cp ../profiles/acme/.env.example ../profiles/acme/.env    # secrets (gitignored)
uv run aiops profile validate acme                        # readable errors, exit 1 if invalid
export AIOPS_PROFILE=acme                                 # or --profile acme on any command
uv run aiops mcp tools logs                               # check each capability's connection
uv run aiops agent run logs "checkout is returning 500s" -s checkout --since 30m
```

| Command | What it does |
|---------|--------------|
| `aiops profile list` | profiles, company, `extends`, whether the catalog is own or inherited |
| `aiops profile show [name]` | summary: chain, catalog, prompt overrides, capability → provider (implemented/planned), required variables |
| `aiops profile show [name] --resolved` | the fully merged configuration as JSON, secrets masked, unset `${VAR}`s left visible |
| `aiops profile validate [name] [--all] [--strict]` | schema + providers + required settings + variables + catalog + `.env.example` coverage |
| `aiops profile init <name> [--from _template\|local] [--company ...]` | new profile folder with metadata, never copies `.env` |
| `aiops profile diff <a> <b>` | every resolved setting and catalog identifier that differs |

Selection order: `--profile` (alias `--env`) > `AIOPS_PROFILE` > `AIOPS_ENV` (deprecated alias,
logs a one-line notice) > `local`. A legacy `config/environments/<name>.yaml` (+
`config/service-catalog/<name>.yaml`) still loads when no `profiles/<name>/` exists.

## The profile files

**`profile.yaml`**: `metadata` (company, description, owners; never inherited), `extends`,
`llm`, `capabilities`, `limits`, `guardrails`, `agents`. Unknown keys are errors, so typos
are caught. `extends: <profile>` inherits another profile: mappings merge, lists and values
replace. Typical use: `acme-staging` extends `acme` and changes a few URLs.

**`services.yaml`**: the service catalog (below). Optional when a parent profile has one;
`extends: <profile>` there merges the parent's catalog service by service.

**`.env.example`**: every `${VAR}` the profile uses, with comments. `aiops profile validate`
warns when one is missing. `${VAR}` = required (loading fails with the list of unset names),
`${VAR:-default}` = optional.

**`prompts/<agent>/vN.md`** (optional): replaces `config/prompts/<agent>/vN.md`, or adds a newer
version (which becomes `latest` unless `agents.<name>.prompt_version` pins one). Every run
records the prompt ref (`logs/v2@<sha>`), so overrides stay auditable.
`aiops prompts list --profile acme` shows where each prompt comes from.

## Capability → provider matrix

Agents bind to **capabilities**; a profile binds each capability to a **provider** and an MCP
server. "Implemented" = works today by configuration only. "Planned" = named in the roadmap;
`validate` reports it as an error so nobody deploys a half-configured profile.

| Capability | Implemented ✓ | Planned | MCP server (implemented) |
|------------|---------------|---------|--------------------------|
| logs | `elasticsearch` ✓ (self-managed or Elastic Cloud, ES\|QL) | `opensearch`, `loki`, `splunk`, `datadog` | `mcp-servers/elasticsearch-mcp` |
| metrics | `prometheus` ✓ (adapter in a later wave) (any Prometheus-compatible API: Prometheus, Thanos, Mimir, VictoriaMetrics, AMP, GMP) | `datadog` | `mcp-servers/prometheus-mcp` |
| alerts | `alertmanager` ✓ | `pagerduty`, `opsgenie` | `mcp-servers/alertmanager-mcp` |
| k8s | `kubernetes` ✓ (EKS, GKE, AKS, OpenShift, Minikube) | – | `mcp-servers/kubernetes-mcp` |
| code | `git` ✓ (read-only clones of GitHub/GitLab/Bitbucket repos) | `github`, `gitlab` (API) | `mcp-servers/git-mcp` |
| tickets | `jira` ✓ (the `mcp-atlassian` tool contract: Jira Cloud / Data Center), `mock` ✓ | – | `sooperset/mcp-atlassian`, `mcp-servers/mock-tickets-mcp` |
| knowledge | `postgres_fts` ✓ (markdown runbooks) | `confluence` | `mcp-servers/knowledge-mcp` |
| LLM | `openai_compat` ✓ (Groq, Gemini, OpenAI, Azure OpenAI, LiteLLM/vLLM gateways) | `anthropic`, `bedrock` | – |

The matrix is code too: `backend/src/aiops/core/profiles.py` (`PROVIDERS`) lists each
provider's status, required settings and the tools its agent calls. Implemented rows come
from the **provider adapter registry** (`backend/src/aiops/providers/`, ADR-0012), so a
provider is "implemented" only when an adapter class exists; `validate` uses it:

```
error: capability logs: provider 'loki' needs setting 'labels.service' (capabilities.logs.settings.labels.service)
error: capability logs: provider 'loki' is planned, not implemented yet (roadmap PR-P2..P4; implemented: elasticsearch)
error: capability tickets: provider 'jira' needs setting 'project_key' (capabilities.tickets.settings.project_key)
warning: capability tickets: tool_allowlist lacks ['jira_search'], which the tickets agent calls (its results will be partial)
warning: service checkout: no logs.index_pattern in the catalog (nor capabilities.logs.settings.index_pattern); the Log agent will skip it
```

A capability the company doesn't have: delete it (or `enabled: false`). Its agent then
reports "not configured" instead of failing the investigation.

## Which settings to change

Everything below is in `profiles/<company>/profile.yaml` → `capabilities.<cap>.settings`
unless noted. `_template/profile.yaml` has each one commented.

| Capability | Setting | What to put there |
|------------|---------|-------------------|
| logs | `fields.{timestamp,level,message,message_keyword,service,environment,trace_id,status_code,endpoint,version,error_type}` | your log field names (ECS: `log.level`, `service.name`, `trace.id`, ...). Check one document with `aiops mcp call logs get_mapping`. |
| logs | `index_pattern` + `service_filter: true` | only if all services share one index family (filters on `fields.service` = catalog `logs.service_value`); otherwise per service in the catalog |
| logs | `error_levels`, `pattern_levels`, `startup_pattern`, `baseline_hours` | your level names, the line your apps print on start-up, the "is this normal?" window |
| logs | `ui_link_template` | Kibana Discover link; `{start}` `{end}` `{kql}` |
| metrics | `labels.{service,namespace,status,k8s_app}` | label **names** (`job`, `app`, `code`, ...) |
| metrics | `metrics.{requests,latency_histogram,db_pool_*,cache_up,memory_rss,restarts,...}` | metric **names** of your instrumentation (e.g. OpenTelemetry `http_server_request_duration_seconds`) |
| metrics | `ui_link_template`, `explore_link_template`, `panels` | Grafana dashboard / Explore links; `{service}` `{namespace}` `{from_ms}` `{to_ms}` `{panel}` `{query}` `{range}` `{end_utc}` |
| alerts | `labels.{service,severity,alertname}`, `critical_severities` | label names on your alert rules, what counts as critical |
| alerts | `ui_link_template` | Alertmanager (or Karma) link; `{filter}` |
| k8s | `default_namespace`, `ui_link_template` | fallback namespace; console link with `{namespace}` `{kind}` `{name}` |
| code | `release_tag_template`, `lookback_hours`, `ui_link_template` | your tag scheme (`v{version}`, `{service}/{version}`), commit link `{repo}` `{sha}` |
| tickets | `project_key`, `resolved_lookback_days`, `symptom_terms`, `ui_link_template` | the incident project, how far back "similar incidents" go, your symptom vocabulary, `{key}` link |
| knowledge | `ui_link_template`, `search_k`, `top_docs` | where a cited runbook opens (`{path}`) |
| every capability | `mcp.url`, `tool_allowlist`, `limits` | your MCP endpoint, the read-only tools agents may call, result/time caps |
| – | `llm.base_url`, `llm.models.{fast,agent,rca}` | your LLM gateway and models (must support tool calling) |

## The service catalog (`services.yaml`)

Every service the agents may investigate, and what it is called in each tool. The planner
never invents a service that isn't in the catalog; ambiguous text leads to a clarifying
question. See `profiles/_template/services.yaml` for a fully commented entry.

| Key | Meaning |
|-----|---------|
| `environments.<env>.aliases` | what people call each environment (`prod`, `prd`, `live` → `production`) |
| `services[].name`, `aliases` | canonical name, and what humans type ("the checkout API") |
| `owners`, `depends_on`, `runbooks` | on-call team/channel, dependencies (agents check them too), runbook paths |
| `capabilities.<cap>` | per-service identifiers: `logs: {service_value, index_pattern}`, `metrics/alerts: {labels}`, `k8s: {deployment, label_selector, container, namespace}`, `code: {repo, paths}`, `tickets: {components, labels}` |
| `environments.<env>.capabilities.<cap>` | per-environment overrides (index patterns, namespaces, label values) |

Generate it rather than typing it: from Backstage `catalog-info.yaml`, a CMDB export, or
Kubernetes labels (`aiops catalog import` is planned in PR-P3). Start with the 5–10 services
that page most.

## Read-only credentials checklist

Agents are read-only by construction (tool allowlists, server-side guardrails, and
credentials). Ask for **read-only** access only:

- [ ] **Logs**: an Elasticsearch API key / role with `read` + `view_index_metadata` on the
      incident index patterns only (no `write`, no `manage`); set the server's
      `LOGS_ALLOWED_INDEX_PATTERNS`.
- [ ] **Metrics**: a query-only token for the Prometheus-compatible API (Grafana Cloud /
      Mimir: a `metrics:read` access policy); `METRICS_ALLOWLIST` narrows metric names.
- [ ] **Alerts**: Alertmanager API behind read-only auth (alertmanager-mcp has no write
      tools; silences are never created).
- [ ] **Kubernetes**: a ServiceAccount bound to a ClusterRole/Role with only
      `get/list/watch` on pods, pods/log, events, deployments, replicasets, services,
      namespaces; **no** secrets, exec, delete, patch (copy `deploy/k8s/rbac/`). Short-lived
      tokens; `K8S_ALLOWED_NAMESPACES` on the server.
- [ ] **Code**: a read-only deploy key or fine-grained token (`contents:read`) used to
      mirror repos for git-mcp.
- [ ] **Tickets**: a Jira user limited to the incident project(s); reads for agents.
      Writes (create/comment) happen only through `aiops approvals` after a human approves;
      set `TICKETS_READ_ONLY_MODE=true` on the server if the company wants no writes at all.
- [ ] **Knowledge**: read access to the runbook repo / space.
- [ ] **LLM**: an API key scoped to the approved models, with a spend limit. Confirm the
      data-processing terms allow log snippets (redaction runs first: `guardrails.redact`).

## Secrets

- Secrets appear in YAML **only** as `${VAR}` references. Never paste a token in a profile.
- Values come from, in order: the shell / secret manager > `profiles/<company>/.env` > the
  repo `.env`. Both `.env` files are gitignored; `init` never copies a `.env`.
- `aiops profile show --resolved` and `diff` mask `api_key` and any key that looks like a
  token, secret, password or credential.
- **This repository is public.** `profiles/*` is gitignored except `local`, `local-k8s` and
  `_template`. Keep a company profile in a **private** repo and point
  `AIOPS_PROFILES_DIR` at the folder that contains it (`extends:` then resolves inside that
  folder too, so copy a parent profile there if you use one). gitleaks runs in CI.
- In Kubernetes (Helm, PR-047) the same variables come from a `Secret`; the profile folder is
  a `ConfigMap`.

## Day-1 checklist

1. [ ] **Inventory** the stack and fill the provider matrix above for the company. Anything
       "planned" → start with the capabilities that are implemented; the rest can be
       disabled on day 1.
2. [ ] `aiops profile init <company> --company "<Name>"`.
3. [ ] **Request read-only credentials** (checklist above).
4. [ ] **Run the MCP servers** next to the stack (compose/Helm), each with its server-side
       guardrails (allowed indices/namespaces/projects, limits).
5. [ ] **Map fields and labels** in `profile.yaml` (logs fields, metric/label names, alert
       labels, tag scheme, link templates).
6. [ ] **Write `services.yaml`** for the top services (aliases, owners, depends_on,
       identifiers per capability and environment).
7. [ ] Fill `profiles/<company>/.env`; `aiops profile validate <company> --strict`.
8. [ ] `aiops mcp tools <cap>` for every capability; then one real question per agent
       (`aiops agent run <agent> ...`) on a recent incident.
9. [ ] **Add runbooks** to the knowledge capability; optional prompt overrides in
       `profiles/<company>/prompts/` (e.g. company vocabulary).
10. [ ] **Write 3–5 eval scenarios from past postmortems** and run `make eval`: this is the
       evidence stakeholders need.
11. [ ] Start read-only, one team, behind SSO (PR-045/047).

## How to add a provider

Agents say **what** they need; a **provider adapter** says **how** to get it from one
vendor (ADR-0012). Adding a vendor = one adapter module + one prompt fragment + profile
settings. Agent code never changes.

```
backend/src/aiops/providers/
  base.py                  Provider (class attributes for the matrix), ToolRequest
  registry.py              PROVIDER_REGISTRY: (capability, provider) -> adapter class
  logs/__init__.py         the logs interface (LogsProvider) + neutral shapes (LogScope, LogWindow, LogTable)
  logs/elasticsearch.py    ES|QL + Kibana KQL links            (registered)
  logs/_skeleton.py        copy-me template                   (NOT registered: leading "_")
config/prompts/providers/<capability>/<provider>/v1.md       query guidance for the LLM
```

1. **Implement the interface.** Copy `providers/<capability>/_skeleton.py` (logs has one) to
   `providers/<capability>/<provider>.py`. Each method is one question the agent asks and
   returns a `ToolRequest(tool, arguments, columns=...)`: the MCP tool to call and its
   arguments in the vendor's language. Results must come back in the capability's
   **neutral shape**; for logs, a `LogTable` whose columns are the names listed in
   `providers/logs/__init__.py` (`window`, `level`, `count`, `msg`, `first_seen`, ...).
   Alias columns in the query, or map vendor names with `ToolRequest.columns`, or override
   `table()`. Also implement the neutral deep link (`ui_link`) and, if useful,
   `scope_note` (extra prompt text, e.g. how to filter a shared index).
2. **Describe it** with class attributes: `capability`, `name` (the value of
   `capabilities.<cap>.provider`), `mcp` (the MCP server), `required` (dotted settings keys
   `validate` insists on), `agent_tools` (tools the agent calls itself; `validate` warns if
   they're not in `tool_allowlist`), `note`, `prompt_fragment`, and vendor field defaults
   (`default_fields` for logs).
3. **Register it:** `PROVIDER_REGISTRY.register(MyProvider)` at the bottom of the module.
   `aiops.providers` imports every non-`_` module, so nothing else needs editing. The matrix
   row flips from "planned" to "implemented" (remove the static planned row in
   `core/profiles.py`).
4. **Prompt fragment:** `config/prompts/providers/<capability>/<provider>/v1.md` with the tool
   names and 2-3 query examples in the vendor's language. It's rendered with the agent's
   prompt variables (e.g. `$index`, `$project_key`) into the agent prompt's
   `$provider_guidance`, and its ref is recorded with the run
   (`logs/v3@<sha>+providers/logs/<provider>/v1@<sha>`). A company can override it in
   `profiles/<company>/prompts/providers/...` like any prompt.
5. **Profile settings:** `capabilities.<cap>.provider: <name>`, the MCP server's `mcp.url`,
   a read-only `tool_allowlist`, and the provider's settings (field names, labels, link
   templates). Add the settings to `profiles/_template/profile.yaml` and the table above;
   `aiops profile validate <company>` reports missing `required` keys.
6. **Contract tests** (`backend/tests/unit/test_providers.py` shows the pattern): golden
   queries for each method, result normalization into the neutral shape, deep links, and
   the registry/matrix/fragment checks (run for every registered provider automatically).
   Then record fixtures against the real backend
   (`backend/tests/fixtures/<capability>-<provider>/S*/`, with a `meta.json` window) and
   replay the agent's scenarios through the new provider: the evals must pass with **zero
   agent change** (PR-P4 does this for a second logs backend).

## Known vendor couplings (P1 audit; P2 progress)

Audit of `backend/src/aiops/{agents,core,mcp,evals}` and `config/prompts` (PR-P1). These are
the places where code, not configuration, still assumes a vendor. They all work today with
the implemented providers; PR-P2 moves them behind provider adapters selected by
`capabilities.<cap>.provider`, so that adding Loki/Datadog/PagerDuty/GitHub needs no agent
change.

| Where | Coupling | P2 direction |
|-------|----------|--------------|
| ~~`agents/log_agent/agent.py:51-58` (`esql_string`, `esql_like`), `:84` (`FROM ... \| WHERE`), `:175`, `:184`, `:199` (`STATS ... BY`), `:265` (`LIKE`)~~ | the Log agent writes **ES\|QL** itself | ✅ **P2:** `providers/logs/elasticsearch.py` (`volume_by_level`, `message_patterns`, `versions_and_startups`, `first_occurrences`) |
| ~~`agents/log_agent/agent.py:159`~~ | calls tool `execute_esql` by name | ✅ **P2:** the adapter's `ToolRequest` names the tool |
| ~~`agents/log_agent/agent.py:31-35` (`DEFAULT_FIELDS`), `:110`~~ | Elasticsearch/ECS field conventions as defaults | ✅ **P2:** `ElasticsearchLogs.default_fields` / `fields()` |
| ~~`agents/log_agent/agent.py:87`, `:136-143`~~ | UI links built from **KQL** | ✅ **P2:** `ElasticsearchLogs.ui_link(levels=..., phrase=...)` |
| ~~`agents/log_agent/patterns.py:39-40`~~ | `like_prefix` for an ES\|QL `LIKE` | ✅ **P2:** `providers/logs.literal_prefix` + `esql_like` in the adapter |
| ~~`agents/tickets_agent/analysis.py:115-145` (`jql_value`, `recency_clause`, `scope_jql`, `keyword_jql`)~~ | the Tickets agent writes **JQL** | ✅ **P2:** `providers/tickets/jira.py` (`scope_search`, `keyword_search`); `mock` subclasses it |
| `mcp/tickets.py:17-26` (`jira_search`, `jira_get_issue`, ... `TICKET_FIELDS`) | the mcp-atlassian tool names and Jira field names | ✅ **P2:** used only by the jira/mock adapter (reads) and the approval executor (writes, `tickets_agent/draft.py`); `Ticket` is the neutral model |
| ~~`agents/alert_agent/agent.py:56` (`matcher_filter`), `:117` (`list_alerts` args)~~ | Alertmanager matcher syntax and tool arguments | ✅ **P2:** `providers/alerts/alertmanager.py` (`alerts_for`, `silences_for`, `ui_link`) |
| ~~`agents/alert_agent/analysis.py:30`~~ | "Alertmanager keeps no history" caveat text | ✅ **P2:** `AlertsProvider.has_history` / `history_note` |
| ~~`agents/code_agent/agent.py:160`, `:176`, `:246`~~ | git-mcp tool names (`list_releases`, `search_commits`, `get_diff`) | ✅ **P2:** `providers/code/git.py` (`releases_of`, `commits_touching`, `diff_of`) |
| `agents/k8s_agent/agent.py:160-180` | kubernetes-mcp tool names | kept: one Kubernetes API everywhere. `providers/k8s/kubernetes.py` is thin (describes the contract for the registry/`validate`); `knowledge/postgres_fts` likewise |
| `core/config.py:105` (`LLMConfig.provider: Literal["openai_compat", "fake"]`) | only OpenAI-compatible LLMs | `anthropic`, `bedrock` providers (moved to PR-P4) |
| `config/prompts/logs/v1.md:18`, `logs/v2.md:28` (ES\|QL examples), `tickets/v1.md:12-20` (JQL), `alerts/v1.md:18` (Alertmanager) | prompts teach one query language | ✅ **P2:** `logs/v3`, `tickets/v2`, `alerts/v2`, `code/v2` are vendor-neutral; guidance in `config/prompts/providers/{logs/elasticsearch,tickets/jira,alerts/alertmanager,code/git}/v1.md` via `$provider_guidance` |
| `evals/runner.py:308`, `:349` | local seeding defaults `http://localhost:9093` / `:9200` (env-overridable) | test harness only; fine |

Not couplings (already configuration): field names, index patterns, label and metric names,
severities, tag schemes, link templates, project keys, allowlists, MCP URLs.

## See also

- `profiles/_template/` (commented profile and catalog), ADR-0011 (profile format)
- MASTER_PLAN §10 (contracts), §14 "Portability (moved earlier)" (PR-P1..P4), §17 (playbook)
- `docs/setup/jira.md`, `docs/setup/logging.md`, `docs/setup/metrics.md` (per-capability setup)
