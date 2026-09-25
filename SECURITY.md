# Security Policy

## Reporting a vulnerability
Please do not open a public issue. Use GitHub's private vulnerability reporting (Security → Report a vulnerability).

## Design principles
- **Read-only by default.** Credentials for every integration are scoped to read access.
- **Human approval** is required for any write action (creating tickets, restarts, rollbacks).
- **Tool allowlists.** Agents can only call explicitly allowed MCP tools. The LLM never runs arbitrary `kubectl`, SQL, `curl` or Git commands.
- **Redaction.** Secrets and PII are redacted from tool output before it reaches an LLM.
- **Prompt-injection defense.** Tool output (logs, tickets, docs) is treated as data, never as instructions.
- **Audit.** Every LLM and tool call is recorded with the investigation, agent, tool, arguments and result status.
- **No secrets in git.** `detect-secrets` runs in pre-commit and `gitleaks` in CI.
