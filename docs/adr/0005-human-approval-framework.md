# ADR-0005: Human approval for write actions; mock tickets MCP with mcp-atlassian's contract

- **Status:** Accepted
- **Date:** 2026-09-25

## Context
UC-04 lets the system create or update Jira tickets from its findings. This is the first write to an external system. MASTER_PLAN §16 requires that nothing is written without an explicit human approval, and that the audit trail shows who approved it. The same framework will later gate remediation proposals, such as Kubernetes changes. Separately, the tickets capability needs a provider that works offline and in CI (no Jira Cloud site there, D6), and we don't want agent code that forks by vendor.

## Decision
- **Tickets contract = mcp-atlassian's.** Our `mock-tickets-mcp` exposes the same Jira tool names, arguments and result shapes as `sooperset/mcp-atlassian` 0.23.x (`jira_search`, `jira_get_issue`, `jira_create_issue`, `jira_update_issue`, `jira_add_comment`), backed by the Postgres schema `tickets`. Switching to Jira Cloud is configuration only (`TICKETS_PROVIDER`, `TICKETS_MCP_URL`). A contract test runs the same calls against both.
- **Two disjoint allowlists per capability.**
  - `tool_allowlist` is the tools agents get.
  - `write_allowlist` is the tools only the approval executor may call.
  - Config validation rejects any tool that appears in both. Agents get toolsets built from `tool_allowlist` only, so a prompt-injected LLM can't even see a write tool, and a call to one is `blocked` before it reaches the server.
- **Lifecycle** (`core/guardrails/approvals.py`):
  1. An `ActionProposal` has action, capability, tool, arguments, reason, risk, requested_by and investigation_id.
  2. A policy check runs: the tool must be in `write_allowlist`, and the target must be inside the configured project. A proposal that passes becomes `PENDING`; one that fails becomes `REJECTED`.
  3. A human approves or denies it (`APPROVED`/`DENIED`), or it passes its TTL and becomes `EXPIRED`.
  4. The `ApprovalExecutor` re-checks the policy and calls the tool through `MCPRegistry.write_toolset`. The result is `EXECUTED` or `FAILED`.
  - Every transition is validated against a state table, stored, and appended to an audit JSONL file: actor, from/to state, a hash of the arguments, and a note.
- **Store:** an `ApprovalStore` interface. The implementation is a JSON file under `.data/` (atomic rewrite); Postgres comes with the evidence store (PR-032). Tests use an in-memory store.
- **Interfaces:**
  - `aiops tickets draft` creates proposals; drafting never writes.
  - `aiops approvals list|show|approve|deny|execute` is where humans decide; approving executes by default.
  - The API/UI (PR-03x) will call the same service.

## Consequences
- Adding a write action is: a tool in `write_allowlist`, a drafting function, and (optionally) policy rules. No agent changes.
- The JSON store is single-host. Concurrent CLI processes are safe for our local use: writes are atomic, last writer wins per file. It is not meant for multiple API replicas; that's what PR-032 is for.
- Approval identity is whatever `--by` says (default: the OS user) until authentication arrives with the API (RBAC in EPIC-015). The audit log records it either way.
- The mock implements a JQL subset. Unsupported JQL fails loudly, so agent JQL that works on the mock also has to stay within standard Jira syntax. The real-Jira half of the contract test runs only when a site is configured.
