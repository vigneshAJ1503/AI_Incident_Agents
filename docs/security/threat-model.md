# Threat model (PR-042)

AI Incident Agents reads production telemetry (logs, metrics, alerts, Kubernetes state, Git,
tickets, runbooks), sends excerpts to a hosted LLM, and proposes write actions (tickets) that
a human approves. This document covers the local deployment and the parts that carry over to
a company deployment. It is written with STRIDE per component and trust boundary. Each
mitigation points to the code and the test that enforce it. Status: **v1.0.0 (PR-042)**.
Revisit it when PR-044 (remediation) and PR-045 (multi-tenancy, RBAC, SSO) land.

## 1. System and trust boundaries

```
 Browser ──(B1)──▶ Web UI (Next.js, :3100) ──(B2)──▶ API (FastAPI, :8000) ──▶ Orchestrator
                   same-origin /api proxy            auth, rate limits          │ planner
                                                     approvals ─────────┐       │ 7 agents + RCA
                                                                        │       ▼
   Human approver ──(B3)── approve/deny ──────────────▶ ApprovalExecutor ─(B6)─▶ write tools (tickets)
                                                                                 │
   LLM provider (hosted) ◀──(B4)── redacted, wrapped tool output ◀── Toolset ◀──(B5)── MCP servers
                                                                                 │ read-only
   Data sources (ES, Prometheus, Alertmanager, K8s, Git, Jira, KB) ◀────────────┘ (B7)
   Fault injection (local only) ◀──(B8)── API /scenarios (flag + auth)
   Evidence store (Postgres) ◀── API/orchestrator: investigations, events, approvals, audit
```

| # | Boundary | What crosses it | Who is on the other side |
|---|----------|-----------------|--------------------------|
| B1 | Browser → Web UI | pages, `/api/*` calls | the engineer (localhost; SSO in PR-045) |
| B2 | Web UI → API | REST + SSE, `X-API-Key` added server-side | the Web UI's proxy |
| B3 | Human → approvals | approve/deny with identity | an approver (named key; OIDC in PR-045) |
| B4 | Toolset → LLM | prompts with redacted tool output | a hosted, third-party model |
| B5 | Toolset → MCP servers | allowlisted read tool calls | our MCP servers (per capability) |
| B6 | Executor → write tools | approved proposals only | ticketing system |
| B7 | MCP servers → data sources | read-only queries | company systems and **attacker-writable data** |
| B8 | API → fault injector | kubectl against the local Minikube | the demo cluster |

**The key assumption:** everything that comes back across B7 is untrusted. Anyone who can
write a log line, a ticket, a runbook, a commit message or an alert annotation can put text in
front of the model. The model is therefore treated as potentially compromised. Its output
is **never** authority for writes, data signals or confidence.

## 2. STRIDE per component

### Web UI (Next.js)
| Threat | Mitigation | Code / test |
|---|---|---|
| **S** Session/identity spoofing | No browser credential exists. The proxy adds the API key server-side (`AIOPS_UI_API_KEY`) and it never reaches the browser. Real user identity comes with SSO in PR-045 | `frontend/src/app/api/[...path]/route.ts` |
| **T** XSS / injected markup from evidence | React escapes by default. CSP: `default-src 'self'`, `object-src 'none'`, `base-uri 'self'`, `form-action 'self'` | `frontend/next.config.ts`; Playwright e2e |
| **I** Clickjacking | `frame-ancestors 'none'` + `X-Frame-Options: DENY` | `next.config.ts` |
| **E** Browser calls to arbitrary origins | `connect-src 'self'` (+ the configured API origin) | `next.config.ts` |

### API (FastAPI)
| Threat | Mitigation | Code / test |
|---|---|---|
| **S** Unauthenticated access | `api.auth`: `X-API-Key` (shared or **named**), constant-time comparison. `Authenticator` interface; OIDC slot for PR-045 | `api/auth.py`; `tests/security/test_api_hardening.py` |
| **S** Approver spoofing (`"by": "the-cto"`) | With auth on, `decided_by`/`requested_by` = the authenticated name, and the body's `by` is ignored. A shared key can't approve (403) | `routes.decided_by`; `test_named_keys_authenticate_and_become_the_approver` |
| **T** Cross-site requests | CORS: exact origins only (`*` refused), no credentials, fixed method/header lists | `ApiConfig._no_wildcard`; `test_cors_origins_must_be_exact` |
| **R** "I didn't approve that" | Approvals audit (who, when, note) + tool-call audit; approver = authenticated identity | `core/guardrails/approvals.py`, `audit.py` |
| **I** Secrets in responses | Redaction before storage; `/health` never returns keys; logs have no query strings | `tests/security/test_secrets_e2e.py` |
| **D** Request floods / expensive runs | Per-client token buckets (investigations, approvals, scenarios), `max_running_investigations`, 64 KB body limit, `Idempotency-Key` for safe retries | `api/security.py`; `test_rate_limits_per_client_and_group`, `test_body_size_limit`, `test_idempotency_key` |
| **D** Stuck/orphaned runs | Reaper fails `running` investigations with no events for 15 min (reason recorded); graceful shutdown cancels and saves | `runner.reap_stuck`, `runner.shutdown`; `tests/unit/test_reliability.py` |
| **E** Fault injection by anyone | Double guard: `AIOPS_ENABLE_FAULTS=1` **and** authenticated. Local cluster only, one scenario at a time (lock) | `routes._require_faults`; `test_fault_endpoints_need_the_flag_and_auth` |
| **I** Integration secrets leak (PR-046) | Write-only: Fernet-encrypted at rest with `AIOPS_SECRETS_KEY` (refused without it), never returned (at most the last 4 chars), audit and logs carry field names only | `core/secrets.py`, `core/integrations.py`; `tests/unit/test_integrations.py` |
| **E** Weakening guardrails from the UI (PR-046) | Allowlists and guardrail settings aren't editable; changes are validated like `profile validate`; save/test need an authenticated caller when auth is on; every change audited (who, field) | `core/integrations.plan_update`, `api/integrations.require_writer`; same tests |
| **S** SSRF via "Test connection" (PR-046) | The API connects to a caller-supplied MCP URL: keep `api.auth` on outside a laptop (API bound to 127.0.0.1 locally); URLs with credentials are refused; the rate limit group `integrations` caps probing | `api/integrations.py`; ADR-0021 |

### Orchestrator and agents
| Threat | Mitigation | Code / test |
|---|---|---|
| **T** Prompt injection steers the model | Output wrapped as data, every `<tool_output>` spelling escaped, heuristics flag `suspected_injection` (evidence, audit, SSE, report) | `core/guardrails/injection.py`, `mcp/toolset.py`; `tests/security/test_prompt_injection.py` (9 payload families) |
| **T** Invented evidence / fake ids | `result_from_report` rejects unknown evidence ids | `agents/base.py`; `test_compromised_model_cannot_escape_the_guardrails` |
| **T** "Report no_signal" overrides data | Deterministic signals are authoritative in `finalize`; RCA confidence is computed | agents' `finalize`; `test_data_signals_cannot_be_overridden` |
| **E** Model calls write tools | Agents only get `tool_allowlist`; write tools live in `write_allowlist` (validation: never both); unknown tools are `blocked` + audited | `core/config.CapabilityConfig`, `Toolset`; same test |
| **D** Runaway loops / cost | `max_steps`, `max_tool_calls`, `max_execution_s`, `max_tokens` per agent; investigation token budget | `agents/base.py`, `orchestrator/engine.py` |
| **D** One source down stalls everything | Per-capability circuit breakers: open after N failures, half-open probe. The gap is a PARTIAL result naming the source | `mcp/breaker.py`; `test_one_source_down_is_partial_and_named_then_short_circuited` |

### LLM provider (external)
| Threat | Mitigation | Code / test |
|---|---|---|
| **I** Secrets/PII leave the company | Redaction **before** the prompt: private keys, connection strings, AWS/GCP/Azure, GitHub/GitLab/Slack/Atlassian, JWT, bearer/basic, secret-named keys; PII configurable. Secrets on by default. Output truncated | `core/guardrails/redaction.py`; `test_redaction_coverage.py`, `test_secrets_e2e.py` |
| **I** Data residency | Provider is configuration (openai_compat, anthropic, Bedrock/Azure regions) | `llm/`, profiles |
| **T** Malicious/erroneous model output | Treated as untrusted (see Orchestrator). Structured output is validated | `llm/structured.py` |

### MCP servers
| Threat | Mitigation | Code / test |
|---|---|---|
| **E** Arbitrary queries (DSL/PromQL/kubectl/git) | Server-side guards per server: read-only query forms, max rows/time range, allowlisted namespaces/repos | `mcp-servers/*/src/*/guards.py` + their tests |
| **E** Kubernetes writes/secrets | Read-only ServiceAccount; no `secrets`, `exec`, `patch` or `delete` | `deploy/k8s/` RBAC |
| **S** A rogue server exposing extra tools | The client offers only allowlisted tools, whatever the server lists | `Toolset.specs`; `test_compromised_model_…` (server exposes `jira_create_issue`, `fetch_url`) |
| **D** Slow/unreachable server | Timeouts per call, connect retries, circuit breaker | `mcp/client.py`, `mcp/breaker.py` |

### Data sources (attacker-writable content)
| Threat | Mitigation |
|---|---|
| **T** Poisoned logs/tickets/runbooks/commits/alerts | Treated as data (above). Flagged evidence is named in the report's open questions, so a human reviews the source |
| **I** Over-collection | Per-capability limits (`max_results`, `max_time_range_hours`), truncation to `max_tool_output_chars` |

### Approvals and write path
| Threat | Mitigation | Code / test |
|---|---|---|
| **E** Write without a human | Only `ApprovalExecutor` holds a `write_toolset`, only for APPROVED proposals; policy + TTL (24 h) | `core/guardrails/approvals.py`; `tests/unit/test_approvals.py` |
| **S/R** Who approved | Authenticated identity (named keys now, OIDC in PR-045); approvals audit log | `routes.decided_by` |
| **T** Proposal tampering after approval | Proposal arguments are stored at proposal time; execution uses the stored ones | `approvals.py` |

### Evidence store, audit log, secrets
| Threat | Mitigation |
|---|---|
| **I** Secrets at rest | Stored evidence, events and audit records are redacted (e2e test, SQLite + Postgres throwaway schema) |
| **T** Log tampering | Postgres audit table (append-only by convention); JSONL fallback. WORM storage is a company deployment concern |
| **I** Secrets in git | `.env` git-ignored; `detect-secrets` pre-commit; gitleaks in CI; tests build fake tokens at runtime |

### Supply chain and CI
| Threat | Mitigation |
|---|---|
| **T** Compromised dependency | Locked (`uv.lock`, `package-lock.json`); `pip-audit` per project and `npm audit --omit=dev` (high+) in `security.yml`, weekly too |
| **T** Vulnerable base images | Pinned tags + Debian/Alpine security updates at build; Trivy fails on fixable HIGH/CRITICAL for every image. The web image drops npm/yarn |
| **T** Hijacked GitHub Action tag | Every action pinned to a commit SHA; Dependabot bumps them (grouped, weekly) |
| **E** Over-privileged CI token | `permissions: contents: read` in every workflow |

## 3. Residual risks (accepted for v1)

| Risk | Why accepted / plan |
|---|---|
| Injection heuristics miss novel phrasings | Detection is advisory. The guarantees are structural (allowlist, approvals, evidence ids, authoritative data). Extend the rules as new payloads appear |
| A compromised model writes misleading **prose** (summary, finding text) | Findings must cite real evidence, and the RCA ranking can't invent hypotheses or change confidence. A human reads the report with the flagged evidence named |
| Shared API key has no identity | It can read and investigate but not approve. Named keys now; OIDC + RBAC in PR-045 |
| Rate-limit buckets and idempotency keys are in-process | One API process per deployment today. A shared store (Postgres/Redis) is needed for replicas |
| Behind the Web UI proxy all users share one client identity | Localhost demo. PR-045 adds per-user tokens |
| Next.js needs `'unsafe-inline'` scripts | Nonce-based CSP needs a middleware; the risk is limited since no user HTML is rendered |
| `apt-get upgrade` / `apk upgrade` at build makes images time-dependent | Security fixes beat bit-for-bit reproducibility. Base tags stay pinned; Trivy runs weekly |
| Heuristic PII redaction (emails/IPs/cards) can miss formats | Configurable per profile. Companies with strict residency choose a regional LLM provider |
| Audit log is not tamper-proof | Company deployments ship it to WORM/SIEM storage |

## 4. How to verify

```bash
cd backend
uv run pytest tests/security -q          # injection, redaction, e2e leak, API hardening
uv run pytest tests/unit/test_reliability.py -q
make audit                               # pip-audit (all projects) + npm audit
```
CI: `ci.yml` (tests, gitleaks) and `security.yml` (pip-audit, npm audit, Trivy on every image).
