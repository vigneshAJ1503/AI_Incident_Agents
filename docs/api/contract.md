# API contract v1 (orchestrator ⇄ API ⇄ UI)

The contract the **orchestrator** (PR-030–034), the **API** (PR-035) and the **Web UI** (PR-036–039) build against in parallel. JSON field names follow the Pydantic models in `backend/src/aiops/core/models.py` (JSON Schemas in `docs/schemas/`). If you need a change, update this file in the same PR and note it in the PR description.

- Base URL: `http://localhost:8000/api` (the UI reads `NEXT_PUBLIC_API_URL`).
- Auth: none locally. An optional API-key header `X-API-Key` (PR-035: `AIOPS_API_KEY`; off by default).
- Times: ISO-8601 UTC. IDs are strings (`inv-…`, `INC-…`, `ev-…`).
- Errors: `{"error": {"code": "not_found", "message": "…"}}` with the matching HTTP status.

## Resources

### Investigation (extends `docs/schemas/Investigation.schema.json`)
`Investigation` as defined today, plus:

```jsonc
{
  "id": "inv-3f2a9c1d7e44",
  "incident": { "id": "INC-1024", "title": "Payment API is returning HTTP 500 in production",
                "service": "payment-service", "environment": "production", "source": "user", "created_at": "…" },
  "context": { "question": "…", "service": "payment-service", "environment": "production",
               "time_range": { "start": "…", "end": "…" }, "symptoms": ["db_timeout_errors_up", "…"] },
  "status": "running | needs_clarification | completed | partial | failed | pending",
  "steps": [ /* InvestigationStep: agent, objective, round, status, started_at, finished_at */ ],
  "results": [ /* AgentResult per agent run: summary, status, findings, evidence, signals, tool_calls, usage, duration_ms */ ],
  "hypotheses": [ /* Hypothesis: statement, confidence, supporting_evidence_ids, contradicting_evidence_ids */ ],
  "recommendations": [ /* Recommendation: action, rationale, risk, requires_approval, evidence_ids */ ],
  "timeline": [ /* TimelineEvent: timestamp, description, source, evidence_id */ ],
  "report": {                                   // NEW (null until the RCA phase finished)
    "summary": "2-4 sentence plain-language summary",
    "root_cause_hypothesis_id": "hy-… | null",  // the top hypothesis; null = no root cause identified
    "confidence": 0.86,                         // of the top hypothesis (0..1); 0 when none
    "impact": "HTTP 500 on POST /api/v1/pay for ~25% of requests since 10:10Z",
    "affected_services": ["payment-service", "order-service"],
    "severity": "critical | high | medium | low | none",
    "next_steps": ["…"],
    "open_questions": ["…"],
    "markdown": "# Incident report …"             // full rendered report (for copy/export/Jira)
  },
  "clarification_question": null,
  "clarification_candidates": [],               // NEW (PR-030): catalog services to pick from
  "claims": [ /* Finding: kind FACT|OBSERVATION|CORRELATION|HYPOTHESIS|RECOMMENDATION, description, evidence_ids */ ],  // NEW (PR-033)
  "versions": { "model": "…", "prompts": "…" },
  "usage": { "input_tokens": 0, "output_tokens": 0, "calls": 0 },   // aggregated (NEW, serialized)
  "duration_ms": 8420,                                               // NEW
  "created_at": "…", "completed_at": "…",
  "mode": "live | replay | demo"                                     // NEW: where the data came from
}
```

`InvestigationSummary`, used in lists, is a subset: `id, incident, status, report.summary, report.severity, report.confidence, affected_services, created_at, completed_at, duration_ms, mode`.

## Endpoints

| Method & path | Purpose |
|---|---|
| `GET /health` | `{status, version, profile, llm: {provider, configured: bool}, capabilities: {logs: "ok|down|disabled", …}}` |
| `GET /services` | Service catalog: `[{name, description, owners, depends_on, environments, runbooks}]` |
| `GET /agents` | Agent registry + health: `[{name, version, description, capabilities, last_run_at, success_rate_7d, p50_ms}]` |
| `GET /dashboard/summary?days=14` | See "Dashboard" |
| `GET /investigations?status=&service=&severity=&q=&limit=50&cursor=` | `{items: InvestigationSummary[], next_cursor}` (newest first) |
| `POST /investigations` | Body `{question, service?, environment?, since?: "30m", start?, end?, mode?: "live|replay"}` → `202 {id, status: "pending"}` |
| `GET /investigations/{id}` | Full `Investigation` |
| `GET /investigations/{id}/events` | **SSE** stream (below); replays past events first, then live ones |
| `POST /investigations/{id}/clarify` | `{answer}`, resumes a `needs_clarification` investigation |
| `POST /investigations/{id}/cancel` | Cancel a running investigation |
| `GET /investigations/{id}/report.md` | `text/markdown` of `report.markdown` |
| `POST /investigations/{id}/tickets/draft` | Creates an approval proposal for a Jira ticket → `{approval_id}` |
| `GET /approvals?status=pending` | `[{id, action, capability, tool, arguments, reason, risk, status, requested_by, investigation_id, created_at}]` |
| `POST /approvals/{id}/approve` · `/deny` | Body `{by, comment?}`. Approve executes the proposal and returns the updated approval (+ `result`, e.g. the ticket key/link) |
| `GET /scenarios` | Demo/dev: `[{id: "S1", title, service, description, active: bool}]` |
| `POST /scenarios/{id}/inject` · `POST /scenarios/revert` | Demo/dev live fault injection. **Only enabled when `AIOPS_ENABLE_FAULTS=1`**, otherwise `403` |

## Live events (SSE on `/investigations/{id}/events`)

Every event: `{"type": "...", "investigation_id": "...", "timestamp": "...", "seq": 17, "agent": "logs" | null, "data": {…}}`.

| type | data |
|---|---|
| `investigation_started` | `{question}` |
| `clarification_needed` | `{question, candidates: ["payment-service", …]}` |
| `plan_created` | `{context, steps: InvestigationStep[]}` |
| `round_started` | `{round, agents: [..]}` |
| `agent_started` | `{step_id, objective, round}` |
| `tool_called` | `{step_id, tool, status, duration_ms}` |
| `evidence_added` | `{step_id, evidence: Evidence}` |
| `agent_finished` | `{step_id, status, summary, signals, evidence_count, duration_ms, tokens}` |
| `rca_started` | `{}` |
| `hypothesis_ranked` | `{hypotheses: Hypothesis[]}` |
| `report_ready` | `{report}` |
| `approval_requested` | `{approval_id, action}` |
| `investigation_finished` | `{status, duration_ms}` |
| `error` | `{message, recoverable: bool}` |
| `heartbeat` | `{}` (every 15 s) |

The stream ends after `investigation_finished`. Clients reconnect with `Last-Event-ID: <seq>`.

## Dashboard (`GET /dashboard/summary?days=14`)
```jsonc
{
  "window_days": 14,
  "totals": { "investigations": 42, "open": 1, "root_cause_found": 35, "no_incident": 5, "failed": 2 },
  "mttr_minutes": { "p50": 3.1, "p90": 7.8 },          // question -> report_ready
  "avg_confidence": 0.81,
  "by_day": [ { "date": "2026-09-15", "investigations": 3, "critical": 1, "high": 1, "medium": 0, "low": 1 } ],
  "by_service": [ { "service": "payment-service", "investigations": 14, "top_root_cause": "DB pool misconfiguration" } ],
  "top_signals": [ { "signal": "db_timeout_errors_up", "count": 11 } ],
  "agents": [ { "name": "logs", "runs": 40, "success_rate": 0.97, "p50_ms": 2100, "tokens": 0 } ],
  "recent": [ /* InvestigationSummary × 5 */ ]
}
```

## Additions requested by the Web UI (PR-036)
Backwards compatible; the UI treats all of them as optional.
- `GET /health` adds `faults_enabled: bool` (true when `AIOPS_ENABLE_FAULTS=1`), so the Scenarios page can disable Inject/Revert with an explanation instead of waiting for a `403`.
- Investigation `status` may be `cancelled` after `POST /investigations/{id}/cancel` (the final `investigation_finished` event carries `{status: "cancelled"}`).
- SSE resume: besides the `Last-Event-ID` header (sent by the browser on its own reconnects), accept `?last_event_id=<seq>` on `/events`; `EventSource` can't set headers when the UI reopens a closed stream.
- SSE framing: the UI listens to both unnamed (`data:` only) and named (`event: <type>`) messages; send `id: <seq>` on every event.

## As built by the API (PR-035)
Backwards compatible additions and clarifications (run/curl guide: [`README.md`](README.md)).
Every response was checked against the Web UI's zod schemas (`frontend/src/lib/api/schemas.ts`).

- **Status codes:** `POST /investigations` and `/clarify` → `202`, `/cancel` → `202`,
  `/tickets/draft` → `201`, `/scenarios/revert` → `202` (runs in the background). Error codes
  per status are listed in the README.
- `InvestigationStatus` gained `cancelled` (also in `docs/schemas/Investigation.schema.json`).
- `InvestigationSummary.report` is `null` until the RCA phase finished (was an object of
  nulls).
- `GET /investigations`: also `mode=live|replay|demo`; `service` accepts catalog aliases;
  `next_cursor` is an opaque keyset cursor (`created_at` + `id`), `null` on the last page.
- `POST /investigations`: optional `scenario: "S1"` (replay that scenario). Response adds
  `mode: "live|replay"` and `scenario`. Without an LLM key (or with a capability down) the
  investigation is a replay of the scenario matching the question/service, else
  `422 no_matching_scenario`; `mode: "live"` then answers `409 live_unavailable`.
- `POST /investigations/{id}/clarify` re-runs the investigation with the same id; its events
  continue the same `seq` (the stream of the first run ended with
  `investigation_finished {status: "needs_clarification"}`).
- `POST /investigations/{id}/cancel` → `{id, status: "cancelled"}`; `409 not_running` when it
  already finished. An investigation waiting for a clarification is closed as `cancelled`.
- `POST /investigations/{id}/tickets/draft`: optional body `{comment_on?, issue_type?,
  requested_by?}`; response adds `status` and `summary` (the drafted title). A policy
  rejection answers `422 policy_rejected`.
- **Approvals:** the framework's `rejected` (policy) and `expired` (TTL) are reported as
  `status: "denied"` with the exact value in the new `lifecycle_status`; also `expires_at`,
  `error`, `policy_reason`. `comment` = the approver's note. `?status=` filters by the exact
  lifecycle value.
- `GET /health` adds `store: "ok|down"`; `status` is `degraded` when the store is down.
  Capabilities come from a cheap cached TCP check (`aiops doctor` is the thorough one).
- `GET /agents` adds `runs_7d` and `enabled` (capabilities enabled in this profile).
- `GET /scenarios` adds `injectable` (S0 has no fault). `inject` → `{scenario, status:
  "injected", message}`; `404` unknown, `422 not_injectable`, `409 fault_error` (e.g. the
  cluster lock is held or another scenario is active).
- **SSE:** `heartbeat` carries the `seq` of the last event sent (it isn't stored, and the UI's
  seq de-duplication drops it). `Last-Event-ID` and `?last_event_id=` both work (the larger
  wins). With an API key configured, `/events` also accepts `?api_key=`.
- `approval_requested` is only published while the investigation is still running.

## Demo data

**As built (PR-034):** `aiops demo seed` stores 46 investigations (6 replays + 40 synthetic, seeded RNG,
`mode: "demo"`). `aiops demo export --out <dir>` (`make demo-export`, default `demo/export/`) writes
`investigations.json` (`{items, next_cursor}`), `investigations/<id>.json`, `events/<id>.json`
(SSE streams), `dashboard.json`, `services.json`, `agents.json`, `approvals.json`,
`scenarios.json` and `manifest.json`; evidence data is trimmed to short excerpts. The UI's committed
dataset (`frontend/src/demo/`, PR-036) has its own layout and generator (`make ui-demo-data`);
this export is the backend-generated equivalent from real replay runs. **PR-039:** `make demo`
serves the seeded store through the real API; `aiops demo seed --if-older-than H` keeps it
idempotent, `--reset` also drops UI-started replays; every seeded timestamp is `<= now`.

`aiops demo seed` (PR-034) runs full investigations in **replay** mode over scenarios S0–S5 (recorded fixtures, fake LLM, zero tokens). It stores them with `mode: "demo"`, plus a synthetic 14-day history (about 40 investigations spread over the services, with realistic statuses, severities and durations) so the dashboard has charts on first launch. The UI also ships a **static demo dataset** (`frontend/src/demo/*.json`, generated from the same contract), so it can run with no backend at all (`NEXT_PUBLIC_DEMO=1`).
