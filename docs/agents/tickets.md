# Tickets (Jira) Agent — UC-03

`aiops agent run tickets "Payment API is returning HTTP 500" -s payment-service`

Finds tickets that relate to an incident: **open known issues**, **open issues on dependencies**
and **similar past incidents**. Package: `backend/src/aiops/agents/tickets_agent/`
(agent `tickets`, capability `tickets`, evidence kind `ticket`, prompt `config/prompts/tickets/v1.md`).

## How it works
1. **Scope (deterministic).**
   - Service and dependency identifiers come from the service catalog: `tickets: {components, labels}` for the service and for its `depends_on` services.
   - Symptom terms come from the question, `context.symptoms` (e.g. the logs agent's `db_timeout_errors_up`) and `hints`, matched against a deliberately specific vocabulary (timeout, http_500, http_503, oom, latency, database, cache, crash, image_pull). Generic words ("failing", "wrong", "error") never become search terms. The vocabulary can be replaced with `capabilities.tickets.settings.symptom_terms`.
2. **Search (1–2 JQL calls, absolute dates so fixtures replay).** Both searches return open tickets, or tickets resolved within `resolved_lookback_days` (default 90).
   - `project = "OPS" AND (component in (...) OR labels in (...)) AND (statusCategory != Done OR resolved >= "<date>") ORDER BY updated DESC`
   - `project = "OPS" AND (text ~ "500" OR ...) AND (...)`, only when symptom terms exist
   - If Jira rejects a component name, the first search is retried with labels only.
3. **Relevance (deterministic).** Each ticket is classified by relation (same service / dependency / other) and whether it mentions a symptom. The resulting categories:

   | Category | Rule | Signal |
   |----------|------|--------|
   | known issue | open, same service, symptom match | `known_issue_open` |
   | dependency known issue | open, dependency, symptom match | `dependency_known_issue_open` |
   | similar past incident | resolved in the look-back, same service, symptom match | `similar_past_incident` |
   | open on service | open, same service, no symptom match (context only) | `related_open_tickets` |
   | none relevant | | `no_related_tickets` |

   The most relevant tickets (up to 8) become individual evidence items. Each one carries the ticket link from `ui_link_template`, its status, labels and components, the matched symptoms and its category.
4. **LLM (bounded).**
   - It gets the overview plus the read tools `jira_search` and `jira_get_issue`, for example to read a known issue's comments or workaround.
   - It writes the evidence-cited summary.
5. **Finalize.**
   - Signals and success/no_signal come from the data.
   - The LLM can't claim a known issue that the data doesn't show (for example in S0), and can't hide one.

## Scenario ground truth (`scenarios/*/agents/tickets.yaml`)
| Scenario | Expected |
|----------|----------|
| S0 healthy | `no_signal`: OPS-12 is visible as context, never claimed as a known issue |
| S1 DB pool | `known_issue_open`: **OPS-12** "payment-service DB connection timeouts" |
| S2 memory leak | `no_signal` from the vague question; with the logs agent's `oom_errors` symptom → `similar_past_incident` OPS-3 |
| S3 slow dependency | `dependency_known_issue_open`: OPS-7 inventory-service slow query (and OPS-12 on payment-service) |
| S4 bad deployment | `no_signal` |
| S5 cache outage | `similar_past_incident`: OPS-5 Redis failover; OPS-12 is not a known issue for "slow" |

## Tests
- Fixtures: `backend/tests/fixtures/tickets/<scenario>/tickets.json`, recorded live from mock-tickets-mcp at FIXED_NOW. Re-record with `make record-tickets-fixtures`.
- Unit (zero tokens): `tests/unit/test_tickets_agent.py` and `tests/unit/test_tickets_analysis.py`; also `make eval AGENT=tickets`.
- Integration: `tests/integration/test_tickets_agent_live.py` against the live mock.

## Known limits
- Symptom matching is lexical: word match with light stemming, the same as the mock's text search. Jira Cloud's text search stems differently, so results can differ slightly.
- Tickets are only as good as their labels and components. Services must be labelled consistently with the catalog.
- Verified against the mock only. Jira Cloud needs a site (docs/setup/jira.md).
