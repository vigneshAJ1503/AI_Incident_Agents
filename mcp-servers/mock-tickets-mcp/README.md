# mock-tickets-mcp

An offline stand-in for Jira (MASTER_PLAN D6). It exposes **the same tool names, arguments and
result shapes as [sooperset/mcp-atlassian](https://github.com/sooperset/mcp-atlassian) 0.23.x**,
so the `tickets` capability can switch between this mock and a real Jira Cloud site by
configuration only (`TICKETS_PROVIDER`, `TICKETS_MCP_URL`). It is the default locally and in CI.

Storage is the Postgres schema `tickets` (psycopg 3, async). The server creates the schema
idempotently at start; `aiops seed tickets` fills it with the deterministic `OPS` backlog.

## Tools

| Tool | Kind | Result (as mcp-atlassian) |
|------|------|---------------------------|
| `jira_search(jql, fields, limit, start_at, projects_filter, ...)` | read | `{total, start_at, max_results, issues: [issue]}` |
| `jira_get_issue(issue_key, fields, comment_limit, include, ...)` | read | `issue` |
| `jira_create_issue(project_key, summary, issue_type, assignee, description, components, additional_fields)` | write | `{message, issue}` |
| `jira_update_issue(issue_key, fields, additional_fields, components, return_fields)` | write | `{message, issue}` |
| `jira_add_comment(issue_key, body, visibility, public)` | write | `{id, body, created, author}` |

`issue` follows `JiraIssue.to_simplified_dict`: `id, key, summary, browse_url, description,
status {name, category, color}, issue_type {name}, priority {name}, resolution {name},
resolutiondate, assignee, reporter, labels, components, created, updated, comments`, filtered by
`fields` (comma-separated, or `*all`). Arguments the mock doesn't need (`expand`, `page_token`,
`use_display_names`, ...) are accepted and ignored so clients stay compatible.

Agents only get the read tools (capability allowlist in `config/environments/*.yaml`). Write
tools are executed by the approval framework (PR-014) after a human approves.

### JQL subset
`project`, `key`, `status`, `statusCategory`, `issuetype`, `priority`, `resolution`
(`= != IN NOT IN IS [NOT] EMPTY`), `labels`, `component` (same), `text`, `summary`,
`description` (`~ !~`, word match with light stemming, `word*` prefix; `text` includes
comments), `created`, `updated`, `resolved` (`> >= < <= =` with `YYYY-MM-DD[ HH:MM]` or
relative `-7d`/`-12h`/`-2w`), `AND OR NOT ( )`, `ORDER BY created|updated|resolved|priority|key|status ASC|DESC`.

Anything else (functions such as `currentUser()`, `WAS`/`CHANGED`, custom fields) is rejected
with a clear tool error instead of being silently ignored.

## Guardrails (server-side)
- `ALLOWED_PROJECTS` (default `OPS`): other projects are invisible (search, get, create).
- `MAX_RESULTS` caps `limit` (default 50, like mcp-atlassian).
- `READ_ONLY_MODE=true`: write tools are not registered at all (same as mcp-atlassian).
- Updates are limited to `summary, description, priority, labels, components, assignee`.

## Configuration (env)

| Variable | Default |
|----------|---------|
| `TICKETS_STORE` | `postgres` (`memory` for demos: empty, not persistent) |
| `PG_HOST` / `PG_PORT` / `PG_USER` / `PG_PASSWORD` / `PG_DATABASE` | `localhost` / `15432` / `aiops` / (empty) / `aiops` |
| `TICKETS_SCHEMA` | `tickets` |
| `ALLOWED_PROJECTS` | `OPS` (`*` = all) |
| `MAX_RESULTS` | `50` |
| `READ_ONLY_MODE` | `false` |
| `BASE_URL` | `http://localhost:8109` (for `browse_url`) |
| `DEFAULT_AUTHOR` | `aiops-agent` (reporter of created issues, author of comments) |

Plain HTTP routes: `GET /health`, and `GET /browse/<KEY>` (a minimal issue page, so evidence
links open somewhere).

## Run
```bash
make infra-up mock-tickets-up seed-tickets       # from the repo root
uv run mock-tickets-mcp --transport http --port 8109   # streamable HTTP at /mcp
```

## Tests
```bash
uv run pytest -q                                       # in-memory store, no Postgres
PG_PASSWORD=aiops-local-only uv run pytest -m integration -o addopts=""   # Postgres, throwaway schema
```
The backend's `tests/integration/test_tickets_contract.py` runs the same calls against this
server and against mcp-atlassian (when a Jira site is configured).
