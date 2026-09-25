# Human approvals for write actions (UC-04)

Nothing in AI Incident Agents writes to an external system without an explicit human approval.
Agents are read-only. They only get `capabilities.<cap>.tool_allowlist`. Write tools live in a
separate `write_allowlist`, which only the approval executor uses. The design is in
[ADR-0006](../adr/0006-human-approval-framework.md).

```
aiops tickets draft ...            ActionProposal ──policy──▶ PENDING ──approve──▶ APPROVED ──▶ EXECUTED
                                                   └▶ REJECTED     ├─deny──▶ DENIED            └▶ FAILED
                                                                   └─24h──▶ EXPIRED
```

## Create a ticket from findings
```bash
# 1. Findings: any AgentResult JSON (or a list, or an Investigation)
cd backend
uv run aiops agent run tickets "Payment API is returning HTTP 500" -s payment-service --json > /tmp/s1.json
#    (or --investigation <id> for .data/investigations/<id>.json, written by the orchestrator later)

# 2. Draft -> a PENDING proposal. Nothing is written yet.
uv run aiops tickets draft --from-result /tmp/s1.json --service payment-service
#    --comment-on OPS-12   adds the findings as a comment on an existing ticket instead
#    --issue-type Bug      (default)

# 3. Review and decide
uv run aiops approvals list --status pending
uv run aiops approvals show act-…            # arguments, reason, history
uv run aiops approvals approve act-… --by alice --note "checked"    # approves AND executes
uv run aiops approvals deny act-… --by alice --reason "duplicate of OPS-12"
uv run aiops approvals approve act-… --no-execute && uv run aiops approvals execute act-…
```

The draft contains the following:
- **Title:** `[service] <first sentence of the lead finding>`.
- **Description (Markdown):** the investigation id, per-agent summaries, typed findings with evidence ids, signals, an evidence table with deep links (Kibana, ticket, …) and suggested follow-ups.
- **Labels:** `aiops` plus the service's catalog labels.
- **Components:** the service's catalog components.
- **Priority:** `High` when an agent found a signal.

## Guarantees (tested in `backend/tests/unit/test_approvals.py`)
- Config validation fails when a tool is in both `tool_allowlist` and `write_allowlist`.
- An agent whose LLM asks for `jira_create_issue` gets `blocked`, and the server never receives the call (`test_agent_can_never_call_a_write_tool`).
- The executor refuses any proposal that is not `APPROVED`: pending, denied, expired, rejected or already executed. It re-checks the policy right before writing.
- Policy: the tool must be in `write_allowlist`, `project_key` / `issue_key` must be in the configured project, and a reason is required.
- Every transition is appended to `.data/approvals-audit.jsonl` with the actor, from/to state, a sha256 of the arguments and a note. The write itself is also in the tool-call audit (`.data/audit.jsonl`, agent `approvals:<approver>`).

## Configuration
```yaml
guardrails:
  approvals_path: .data/approvals.json            # proposals (JSON file store; Postgres in PR-032)
  approvals_audit_path: .data/approvals-audit.jsonl
  approval_ttl_hours: 24
capabilities:
  tickets:
    tool_allowlist: [jira_search, jira_get_issue]                          # agents
    write_allowlist: [jira_create_issue, jira_add_comment, jira_update_issue]  # approvals only
    settings: { project_key: OPS }
```
Server-side guardrails still apply to approved writes. `mock-tickets-mcp` only allows `ALLOWED_PROJECTS` and only updates a small set of fields. mcp-atlassian uses `JIRA_PROJECTS_FILTER` and `ENABLED_TOOLS`, and `READ_ONLY_MODE=true` removes write tools from either server.

## Not yet
- Approver identity is the `--by` value (default: the OS user) until the API adds authentication and RBAC.
- UC-04's "investigation link as a comment" needs the investigation UI URL (API/UI PRs). Until then, the created ticket's description carries the investigation id.
