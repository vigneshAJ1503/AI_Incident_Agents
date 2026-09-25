# Tickets: mock (default) or Jira Cloud

The `tickets` capability has two providers that expose **the same MCP tools and shapes**:

| Provider | Server | Port | When |
|----------|--------|------|------|
| `mock` (default) | our `mcp-servers/mock-tickets-mcp` (Postgres schema `tickets`) | 8109 | local, offline, CI |
| `jira` | [`sooperset/mcp-atlassian`](https://github.com/sooperset/mcp-atlassian) `ghcr.io/sooperset/mcp-atlassian:0.23.1` | 8102 | a real (free) Jira Cloud site |

Agents only bind to `tickets`; switching is configuration only.

## Mock (default)
```bash
make infra-up             # Postgres
make mock-tickets-up      # builds + starts mock-tickets-mcp (creates schema `tickets`)
make seed-tickets         # OPS backlog: OPS-12 (open payment DB timeouts), OPS-3 (OOM), ...
cd backend && uv run aiops mcp call tickets jira_search \
  --args '{"jql": "project = OPS AND labels = payment-service AND statusCategory != Done"}'
```
Links such as `http://localhost:8109/browse/OPS-12` open a minimal page served by the mock.

## Real Jira Cloud (free)
> Not verified live in this repository (no Jira site on the build machine). The compose service,
> configuration and contract test are ready; run the contract test once after setup.

1. **Create a free site:** <https://www.atlassian.com/software/jira/free> → e.g. `your-name.atlassian.net`
   (Free plan: up to 10 users).
2. **Create the project:** a Scrum/Kanban *software* project with key **`OPS`**. Add components
   `payments`, `orders`, `identity`, `inventory` (Project settings → Components). The agent
   searches by these components and the service labels (`payment-service`, ...) from
   `config/service-catalog/local.yaml` (`tickets: {components, labels}`).
3. **Create an API token:** <https://id.atlassian.com/manage-profile/security/api-tokens>.
   Prefer a dedicated user that only has access to `OPS` (least privilege).
4. **`.env`:**
   ```bash
   JIRA_URL=https://your-name.atlassian.net
   JIRA_USERNAME=you@example.com
   JIRA_API_TOKEN=<token>
   TICKETS_PROVIDER=jira
   TICKETS_MCP_URL=http://localhost:8102/mcp
   TICKETS_UI_URL=https://your-name.atlassian.net
   ```
5. **Start mcp-atlassian:** `make jira-mcp-up` (compose profile `jira`, port 127.0.0.1:8102).
   It runs with `ENABLED_TOOLS=jira_search,jira_get_issue,jira_create_issue,jira_add_comment,jira_update_issue`
   and `JIRA_PROJECTS_FILTER=OPS`. Set `TICKETS_READ_ONLY_MODE=true` to remove the write tools
   from the server entirely.
6. **Create some tickets** (e.g. an open Bug "payment-service DB connection timeouts" with label
   `payment-service`, component `payments`, description mentioning HTTP 500), then verify:
   ```bash
   cd backend && uv run aiops mcp tools tickets
   JIRA_URL=https://your-name.atlassian.net uv run pytest -m integration -o addopts="" \
     tests/integration/test_tickets_contract.py
   ```
7. Switch back to the mock by removing the `TICKETS_*` overrides from `.env`.

## Safety
- Agents get only `jira_search` and `jira_get_issue` (`capabilities.tickets.tool_allowlist`).
- Writes (`jira_create_issue`, `jira_add_comment`, `jira_update_issue`) are only executed by the
  approval framework after an explicit human approval: see [approvals](../guardrails/approvals.md).
- Both servers restrict visible projects server-side (`ALLOWED_PROJECTS` / `JIRA_PROJECTS_FILTER`).
- The API token lives only in `.env` (git-ignored); the container port is bound to 127.0.0.1.
