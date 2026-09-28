# Security Policy

## Reporting a vulnerability
Please don't open a public issue. Use GitHub's private vulnerability reporting (Security → Report a vulnerability).
We aim to acknowledge reports within 5 working days. Include the affected component, the steps to reproduce and the impact.

## Supported versions
Only `main` (and the latest `v1.x` tag) receives security fixes.

## Design principles
The full STRIDE threat model is [docs/security/threat-model.md](docs/security/threat-model.md). It covers trust boundaries, mitigations mapped to code and tests, and residual risks.

- **Read-only by default.** Credentials for every integration are scoped to read access. Kubernetes access uses a read-only ServiceAccount (no secrets, exec, patch or delete).
- **Human approval** is required for every write action (tickets now; restarts and rollbacks in PR-044).
  - Only the approval executor can call write tools.
  - With API auth on, the approver is the authenticated identity, never a self-declared name.
- **Tool allowlists.** Agents only see and call allowlisted read tools. Write tools are in a separate `write_allowlist`. MCP servers enforce their own server-side guards. The LLM never runs arbitrary `kubectl`, SQL, `curl` or Git commands.
- **Redaction.** Before tool output reaches an LLM, the evidence store, the audit log, SSE or the API, redaction removes:
  - secrets: private keys, connection strings, cloud and SaaS tokens, JWT, bearer tokens and secret-named keys (on by default)
  - PII: emails, IPs and cards (configurable)

  See [docs/guardrails/prompt-injection-and-redaction.md](docs/guardrails/prompt-injection-and-redaction.md).
- **Prompt-injection defense.**
  - Tool output is data, wrapped in `<tool_output>`, and fake delimiters are escaped.
  - Suspicious output is flagged `suspected_injection` in the evidence, the audit log and the report.
  - Findings may only cite real evidence ids.
  - Deterministic data signals override the model.
- **API boundary.** Optional API-key auth (named keys = identities; OIDC in PR-045), per-client rate limits, request size limits, strict CORS, security headers and CSP. Fault injection is double-guarded (env flag + auth).
- **Reliability.** Per-capability circuit breakers turn a dead data source into a named gap in a PARTIAL report. Also: idempotent investigation creation, graceful shutdown, and a reaper for stuck investigations.
- **Audit.** Every LLM and tool call is recorded with the investigation, agent, tool, (redacted) arguments, result status and injection flags. Approvals have their own audit trail.
- **No secrets in git.** `detect-secrets` runs in pre-commit and `gitleaks` in CI. Tests build fake tokens at runtime.
- **Supply chain.** Dependencies are locked. GitHub Actions are pinned to commit SHAs. Container base images are pinned and patched at build. `security.yml` runs `pip-audit` for every Python project, `npm audit` and Trivy on every image, on each PR and weekly. Dependabot opens grouped weekly updates. Accepted risks live in `.github/security/pip-audit-ignore.txt` and `.trivyignore`, each with a reason and a review date.

## Running the checks locally
```bash
cd backend && uv run pytest tests/security -q   # prompt injection, redaction, e2e leak, API hardening
make audit                                     # pip-audit (all projects) + npm audit
```
