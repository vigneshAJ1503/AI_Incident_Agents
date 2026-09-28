# The aiops API (PR-035)

REST + Server-Sent Events over the investigation orchestrator, for the Web UI. The contract
(endpoints, shapes, events) is [`contract.md`](contract.md); design notes are in
[ADR-0015](../adr/0015-api-background-runs-and-sse-replay.md).

- Code: `backend/src/aiops/api/` (FastAPI). OpenAPI: <http://127.0.0.1:8000/api/docs>
  (JSON at `/api/openapi.json`).
- Profile-driven: the API loads the profile named by `AIOPS_PROFILE` (default `local`);
  capabilities, catalog, store and approvals all come from it. Nothing is vendor-specific.
- Zero cost: without an LLM key every investigation is a **replay** of recorded fixtures
  (zero tokens).

## Run it

```bash
make infra-up                 # Postgres (+ the data stack); the store needs Postgres
make mcp-up                   # only for live mode / approvals (mock-tickets-mcp)
make api                      # = cd backend && uv run aiops serve --host 127.0.0.1 --port 8000
make api RELOAD=1             # auto-reload while developing
uv run aiops serve --profile local-k8s --port 8001   # another profile / port
```

The whole product (API + Web UI + seeded Postgres + mock tickets) in one command:
`make demo` ([docs/setup/demo.md](../setup/demo.md)). As a container (opt-in, not part of `make infra-up`):

```bash
make api-up                   # builds aiops/backend:0.1.0, starts aiops-api on 127.0.0.1:8000
make api-logs                 # JSON logs (one access-log line per request, with request id)
make api-down
```

The container (`backend/Dockerfile`, multi-stage, `python:3.12.13-slim-bookworm`, UID 10001,
read-only root filesystem, `mem_limit: 320m`) measured **~120 MiB** idle and after a replay
investigation (`docker stats`). Inside the `aiops` network it reaches the MCP servers and
Postgres by container name (`deploy/compose/docker-compose.app.yml`).

## Configuration

| Setting (profile `api:`) | Env var | Default | What |
|---|---|---|---|
| `cors_origins` | `AIOPS_API_CORS_ORIGINS` | `http://localhost:3000`, `:3100` (+127.0.0.1) | Browser origins (the Web UI). Exact origins only: `*` or paths are refused |
| `auth` | | `auto` | `auto` (= `api_key` when a key is set, else `none`), `none`, `api_key`; `oidc` is reserved for PR-045 |
| `api_key` | `AIOPS_API_KEY` | empty = off | Shared secret: `X-API-Key` on every `/api` route except `/api/health` and the docs; SSE also accepts `?api_key=`. No identity |
| `api_keys` | `AIOPS_API_KEYS` | empty | Named keys `name:KEY,...` (16+ chars): each is an identity (approvals, rate limits) |
| `rate_limits` | | investigations `10/minute`, approvals `30/minute`, scenarios `6/minute` | Token bucket per client (key name, else peer IP) -> `429 rate_limited` + `Retry-After` |
| `max_body_bytes` | | 65536 | Larger bodies -> `413 payload_too_large` |
| `idempotency_ttl_s` | | 86400 | How long an `Idempotency-Key` of `POST /investigations` is remembered |
| `stuck_after_s` / `reaper_interval_s` | | 900 / 60 | Reaper: `pending`/`running`, not running here, no event for 15 min -> `failed` (reason in an `error` event); 0 = off |
| `max_running_investigations` | | 2 | Per API process; more -> `429 too_many_investigations` |
| `heartbeat_s` | | 15 | SSE keep-alive |
| `health_cache_s` | | 30 | `/health` caches capability reachability |
| | `AIOPS_ENABLE_FAULTS` | `0` | `1` enables `POST /api/scenarios/{id}/inject` and `/revert` (else 403). Also needs API auth (double guard) |
| | `AIOPS_UI_API_KEY` | empty | Web UI container: the `X-API-Key` its server-side proxy adds (never reaches the browser) |
| | `AIOPS_FAULTS_KUBE_CONTEXT` | `aiops` | kubectl context of the fault endpoints |
| | `AIOPS_REPLAY_TOOL_DELAY_S` | `0` (compose: `0.5`) | Seconds per recorded tool call of a replay, so the Web UI's live view animates (max 10) |

Ports bind to `127.0.0.1`. Keys are never returned (`/health` only says whether an LLM is
configured). Logs never contain query strings (they may carry `api_key`).

## Security

The threat model is `docs/security/threat-model.md`. The API's part of the boundary (PR-042):

- **Authentication** (`api/auth.py`): one `Authenticator` interface. It returns a
  `Principal(subject, method, identified, tenant, roles)`. `none` (local) and `api_key` exist
  today. `OIDCAuthenticator` is the PR-045 slot (a bearer JWT verified against JWKS, `iss`,
  `aud` and `exp`, which becomes a Principal with tenant and roles). `api.oidc` can already
  be written, but `api.auth: oidc` is refused until PR-045.
- **Approvals need an identity** when auth is on:
  - Only a *named* key (`AIOPS_API_KEYS`) may approve or deny.
  - `decided_by` / `requested_by` are the key's name, and the body's `by` is ignored.
  - The shared `api_key` gets `403 approver_identity_required`.
  - With auth off (local), the self-declared `by` is kept.
- **Fault endpoints** need `AIOPS_ENABLE_FAULTS=1` **and** an authenticated caller.
  `/health` reports `faults_enabled: true` only when both hold. `make demo-live` generates
  a one-off named key for the Web UI's proxy.
- **Rate limits, body size, idempotency**: see the table above. `POST /investigations`
  with `Idempotency-Key: <8-128 of [A-Za-z0-9._:-]>` is safe to retry:
  - the same key and body return the same investigation (`Idempotent-Replayed: true`);
  - a different body returns `422 idempotency_key_reused`.
- **Headers** on every response: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`
  (SSE: `no-cache`) and a CSP of `default-src 'none'; frame-ancestors 'none'`
  (`/api/docs` may load Swagger UI from jsDelivr). The Web UI sends its own CSP
  (`frontend/next.config.ts`).
- **In-process state**: rate-limit buckets and idempotency keys live in the API process.
  One API process per deployment today; a shared store is the multi-replica follow-up.

## Live or replay?

`POST /api/investigations` picks the mode:

1. `scenario: "S1"` in the body -> **replay** that scenario.
2. An LLM key + agent model configured **and** every enabled capability reachable (a cheap,
   cached TCP check; `aiops doctor` is the thorough one) -> **live** (unless `mode: "replay"`).
3. Otherwise **replay** of the scenario whose question matches, or whose service the question
   (or `service`) names. Nothing matches -> `422 no_matching_scenario` listing the scenarios.
   `mode: "live"` when live isn't possible -> `409 live_unavailable` with the reasons.

## curl examples

```bash
API=http://127.0.0.1:8000/api

curl -s $API/health | jq
curl -s $API/services | jq '.[].name'
curl -s "$API/dashboard/summary?days=14" | jq .totals
curl -s "$API/investigations?status=completed&service=payment-service&limit=5" | jq '.items[].id, .next_cursor'
curl -s "$API/investigations?q=redis" | jq '.items[] | {id, status, report}'

# start one (no LLM key here: a replay of S1, zero tokens)
ID=$(curl -s -X POST $API/investigations -H 'content-type: application/json' \
  -d '{"question": "Payment API is returning HTTP 500 in production"}' | jq -r .id)

# stream it live (-N: no buffering); ends after investigation_finished
curl -N $API/investigations/$ID/events
#   id: 1
#   event: investigation_started
#   data: {"type":"investigation_started","investigation_id":"inv-…","seq":1,…}
#   …
#   event: investigation_finished

# reconnect after event 100 (the browser sends Last-Event-ID itself)
curl -N -H 'Last-Event-ID: 100' $API/investigations/$ID/events
curl -N "$API/investigations/$ID/events?last_event_id=100"

curl -s $API/investigations/$ID | jq '{status, mode, report: .report.summary}'
curl -s $API/investigations/$ID/report.md

# a named scenario, cancel, clarification
curl -s -X POST $API/investigations -H 'content-type: application/json' \
  -d '{"question": "Is payment-service healthy?", "scenario": "S0"}'
curl -s -X POST $API/investigations/$ID/cancel
curl -s -X POST $API/investigations/$ID/clarify -H 'content-type: application/json' \
  -d '{"answer": "payment-service"}'

# ticket draft -> human approval -> executed through ApprovalExecutor (mock-tickets-mcp)
AID=$(curl -s -X POST $API/investigations/$ID/tickets/draft | jq -r .approval_id)
curl -s "$API/approvals?status=pending" | jq '.[] | {id, tool, reason}'
curl -s -X POST $API/approvals/$AID/approve -H 'content-type: application/json' \
  -d '{"by": "alice", "comment": "looks right"}' | jq '{status, result}'
# or: .../deny -d '{"by": "alice", "comment": "duplicate"}'

# scenarios (inject/revert only with AIOPS_ENABLE_FAULTS=1, else 403)
curl -s $API/scenarios | jq '.[] | {id, active}'
curl -s -X POST $API/scenarios/S1/inject
curl -s -X POST $API/scenarios/revert

# with an API key configured
curl -s -H "X-API-Key: $AIOPS_API_KEY" $API/services
```

## Errors

Always `{"error": {"code": "...", "message": "..."}}` with the HTTP status:

| Status | Codes |
|---|---|
| 400 | `invalid_cursor` |
| 400 | `invalid_idempotency_key` |
| 401 | `unauthorized` (API key) |
| 403 | `faults_disabled`, `faults_need_auth`, `approver_identity_required` |
| 413 | `payload_too_large` |
| 404 | `not_found`, `unknown_scenario` |
| 409 | `live_unavailable`, `already_running`, `not_running`, `not_waiting_for_clarification`, `report_not_ready`, `nothing_to_draft`, `invalid_state` (approval), `fault_error`, `capability_disabled` |
| 422 | `validation_error`, `no_matching_scenario`, `policy_rejected`, `not_injectable`, `idempotency_key_reused` |
| 429 | `too_many_investigations`, `rate_limited` (with `Retry-After`) |
| 503 | `store_unavailable` (Postgres down; `/health` says `degraded`), `shutting_down` |

Every response carries `X-Request-ID` (sent back when the caller supplies a sane one).

## Tests

- `tests/unit/test_api.py`: httpx `AsyncClient` on the ASGI app with a SQLite store,
  in-memory approvals, fake fault controller/executor (zero tokens).
- `tests/unit/test_api_contract.py`: every response over the demo history + a replay
  validates against `docs/schemas/*.schema.json` and the pydantic models of
  `aiops.api.models` (which mirror `contract.md`).
- `tests/integration/test_api_live.py` (`make test-integration`): compose Postgres in a
  temporary schema, a real uvicorn server for SSE, S1 scored against its ground truth,
  pagination/dashboard, a ticket approved end to end through mock-tickets-mcp, 403 on faults.
