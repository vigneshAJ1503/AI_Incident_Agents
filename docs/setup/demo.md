# The one-command demo (`make demo`)

`make demo` starts the **real product**: the Web UI, the REST + SSE API, Postgres with a
14-day investigation history already in it, and the mock Jira server that approved tickets
are written to. It needs Docker only (no Python or Node on the host), runs at zero cost and
uses **about 0.3 GB** of memory.

```bash
make demo          # build (first run ~3 min, then cached), seed, start; opens http://localhost:3100
make demo-stats    # memory per container + total
make demo-down     # stop everything (data kept), incl. Minikube if demo-live started it
make demo-reset    # wipe the demo investigations + approvals, re-seed, start again
make demo-e2e      # Playwright against the running demo (real API, see below)
```

`NO_OPEN=1 make demo` skips opening the browser (it never opens in CI, or outside macOS).

![make demo: the dashboard over the seeded history](../ui/screenshots/make-demo.png)

## What starts

| Container | Port (127.0.0.1) | Role | Measured memory |
|---|---|---|---|
| `aiops-web` | 3100 | Web UI (Next.js standalone, `frontend/Dockerfile`, UID 10001, read-only FS, `mem_limit` 192m) | 39–59 MiB |
| `aiops-api` | 8000 | `aiops serve` (`backend/Dockerfile`, `mem_limit` 320m) | 150–156 MiB |
| `aiops-postgres` | 15432 | the evidence store + the mock tickets | 45–53 MiB |
| `aiops-mock-tickets-mcp` | 8109 | executes approved Jira drafts (tickets `OPS-n`) | ~58 MiB |
| **Total** | | | **292 MiB idle, 314–326 MiB after the e2e suite** |

Measured with `docker stats --no-stream` on 2026-09-28 (Docker Desktop, Apple silicon).
Images: `aiops/web:0.1.0` 290 MB, `aiops/backend:0.1.0` 416 MB.

Not started (not needed for the demo): Elasticsearch, Prometheus, Alertmanager, Redis, the
other MCP servers and Minikube. Without an LLM key and without those data sources, every
investigation is a **replay** of the recorded fixtures (fake LLM, zero tokens). The dashboard's
status strip shows them as "not running (replays use recorded data)", not as an outage.

## What `make demo` does (`scripts/demo.sh up`)

1. Creates the `aiops` Docker network if needed and starts Postgres.
2. Builds `aiops/backend`, `aiops/web` and `aiops/mock-tickets-mcp` (cached after the first run).
3. `aiops db upgrade` + `aiops demo seed --if-older-than 12`, **inside the API image**:
   idempotent. The history is only re-seeded when the newest demo investigation is older
   than 12 h (`DEMO_SEED_MAX_AGE_H`), so the charts stay full ("the last 14 days" ends now)
   while links to investigations you opened today keep working.
4. Starts mock-tickets-mcp, then the API and the Web UI, waits until both are healthy
   (`docker compose up --wait`) and until `http://localhost:3100/api/health` answers.

The seeded history: S0–S5 replayed end to end + ~40 synthetic investigations over 14 days
(seeded RNG) with live-like durations and a few partial/failed runs. Every timestamp is
consistent and never in the future (`created_at ≤ completed_at ≤ now`, the steps and tool
calls inside the run; PR-039 fixed replays whose run timestamps were shifted by the data's
offset).

## Try it

1. **Dashboard**: 14 days of history, root causes by service, top signals, agent latency.
2. **Investigations** → open any report: root cause, confidence, evidence drawer, timeline.
3. **New investigation** → "Payment API is returning HTTP 500 in production". The API
   replays S1 and streams it over SSE: the agent lanes animate for ~10 s
   (`AIOPS_REPLAY_TOOL_DELAY_S=0.5` s per recorded tool call; `0` = instant), then the report.
   The replay is reported on **today's clock**: its window ends now and every timestamp of
   the recorded data (evidence summaries, findings, timeline, report text, dates like
   "created 2026-09-22") moves by the same offset, so no recording date shows up. The
   agents still query the recorded window (fixtures match on it). Two things keep the
   recording time on purpose: evidence **deep links** (they open the source system where
   the data really is) and the raw evidence `data` payload (the recorded tool output).
   Evals and tests keep the recording clock (deterministic).
4. **Create Jira ticket** on the report → review the draft → **Approve**: the approval
   framework executes it through mock-tickets-mcp and the toast shows `Created OPS-n`
   (browse it at `http://localhost:8109/browse/OPS-n`).

## How the browser reaches the API

The browser talks to **one origin**, `http://localhost:3100`. The container is built with
`NEXT_PUBLIC_API_URL=/api`, and the route handler `frontend/src/app/api/[...path]/route.ts`
forwards `/api/*` to `AIOPS_API_INTERNAL_URL` (read at runtime; compose:
`http://aiops-api:8000`). The response body is streamed through untouched with
`Cache-Control: no-cache, no-transform`, so SSE stays live and Next's compression never
buffers it. Why a proxy rather than `http://localhost:8000/api` in the browser: no CORS, one
port to open, and the same image works wherever the API lives (only a runtime env var
changes). `make ui-dev` still calls `http://localhost:8000/api` directly (CORS allows 3100).

## `make demo-live`: the full stack with real incidents

```bash
make demo-live     # ~2.9 GB: data stack + Minikube (2.2 GB) + all MCP servers + API with faults
```

Starts Elasticsearch, Postgres, Redis, Prometheus, Alertmanager, Minikube with the sample
services, every MCP server, and the API **on the host** with `AIOPS_ENABLE_FAULTS=1` and
`AIOPS_PROFILE=local-k8s` (fault injection runs `kubectl --context aiops`, which the API image
doesn't have); the web container proxies to it via `host.docker.internal`. The **Scenarios**
page can then inject S1–S5 into the cluster (one at a time, `.data/cluster.lock`) and revert.

- **Live investigations need a free hosted LLM key** in `.env`: `OPENAI_COMPAT_API_KEY` with
  Groq (`https://api.groq.com/openai/v1`) or Gemini
  (`https://generativelanguage.googleapis.com/v1beta/openai/`) plus `LLM_MODEL_*`
  (`docs/setup/zero-cost.md`). Without a key, investigations run in **replay** mode, even
  against the live cluster.
- Memory, measured right after `make demo-live` (`make demo-stats`): **2833 MiB** in Docker
  (Minikube node 1.23 GiB, Elasticsearch 976 MiB, Prometheus 94 MiB, 7 MCP servers ~53–58 MiB
  each, Postgres 51 MiB, web 39 MiB, Alertmanager 18 MiB, Redis 12 MiB) + the host API
  (~125 MiB RSS): **≈ 2.9 GB**, inside the 5 GB budget.
- Verified through the web origin: `/api/health` all 7 capabilities `ok`, `faults_enabled:
  true`; `POST /api/scenarios/S1/inject` → `injected`, then `/api/scenarios/revert` back to the
  healthy baseline in ~12 s.
- `make demo-down` stops all of it, including Minikube (kept, not deleted).
- **Kubernetes credentials:** when `demo-live` (re)starts Minikube it writes a fresh read-only
  kubeconfig (`.data/k8s/aiops-reader.kubeconfig`); otherwise it keeps the existing one only if
  the cluster still accepts its token (a `TokenReview`), it has > 2 h left, and the cluster CA is
  unchanged. kubernetes-mcp mounts `.data/k8s` and re-reads the file when it changes: no restart.
- **Code changes arrive through git:** `demo-live` keeps `.data/sample-repo` (read by git-mcp) a
  **healthy** S0 history ending now (rebuilt when older than `DEMO_SEED_MAX_AGE_H`, kept while a
  fault is active). Injecting a scenario also commits its change there with the current time,
  like a real deploy (S1: `tune db pool` by Jordan Lee, `DB_POOL_SIZE "20" -> "2"` in
  `services/payment-service/config/app.yaml`, then `release payment-service v1.8.2`); reverting
  commits a `git revert`. Local only, never pushed. Opt out: `AIOPS_FAULT_GIT_COMMITS=0` (or
  `aiops fault inject S1 --no-git-commit`).
- **A freshly started cluster** has no metric history before the incident window. The Metrics
  agent then compares against absolute thresholds (5xx > 5 %, p95 > 2 s, DB pool ≥ 90 %, DB
  waiters > 0, cache down; `capabilities.metrics.settings.absolute_thresholds`), reported as
  "no baseline; above the absolute threshold X", instead of "No metric anomaly".

## `make demo-e2e`: Playwright against the real API

`frontend/playwright.real.config.ts` + `frontend/e2e/real/demo.spec.ts`, run against a
running `make demo` (no web server of its own):

1. `/api/health` through the web origin (the proxy), store `ok`;
2. the dashboard shows the seeded history (≥ 40 investigations, bars rendered);
3. a stored S1 report opens (root cause "connection pool", confidence);
4. a new S1 replay: live SSE lanes → report with the root cause → Jira draft → approve →
   `Created OPS-n` from mock-tickets-mcp.

4 tests, ~16 s. It is **not a CI job**: it needs the three images built and a seeded Postgres
(several minutes on a runner for one suite). CI keeps the demo-mode Playwright suite
(`make ui-e2e`), and the API is covered there by `test_api*.py`. Run `make demo-e2e` before a
release tag.

## Two demo datasets, one story

| | Static UI dataset | `aiops demo seed` |
|---|---|---|
| Where | `frontend/src/demo/*.json` | Postgres (schema `investigations`, `mode: "demo"`) |
| Used by | `NEXT_PUBLIC_DEMO=1` (no backend; UI e2e in CI, screenshots) | `make demo` (the real API) |
| Made by | `frontend/scripts/generate-demo.ts` (deterministic; CI checks freshness) | real replays of the recorded fixtures |

They keep their own layouts (the static one is hand-tuned for a backend-less UI; the seed is
real pipeline output), and `backend/tests/unit/test_demo_datasets.py` keeps them telling the
**same story**: every static scenario S0–S5 must pass the replays' ground truth
(`scenarios/*/expected.yaml`: service, root-cause keywords, minimum confidence, **severity**,
no false positive on S0) and may only name release versions that exist in that scenario's fixtures
(this caught S2's `v2.3.1`; the fixtures say `v2.3.0`). `aiops demo export` stays the
backend's contract-shaped export for other consumers.

**Severity** is rated in ONE place: the response builder, from `orchestrator.severity` in
the profile (`aiops.core.config.SeverityRules`): `critical` = users get errors (measured
peak 5xx ratio >= `critical_error_rate`, 5%) on a service whose catalog `tier` is in
`critical_tiers` (payment-service is `tier: 1`) with a confident root cause; `high` = other
user-facing impact or a critical alert; `medium` = latency only; `low` = the rest. So S1 is
`critical` and S2–S5 `high` in both datasets (each scenario's `investigation.severity`).

## Troubleshooting

- Port 3100 or 8000 taken: `WEB_PORT=3200 API_PORT=8200 make demo`.
- "Demo data is fresh … kept it": expected on re-runs; `make demo-reset` re-seeds now.
- The API is unreachable from the UI: `docker logs aiops-web` (the proxy answers
  `502 api_unreachable` with the upstream URL) and `make api-logs`.
- K8s agent: "unauthorized: the ServiceAccount token expired or is invalid": run
  `make k8s-reader-kubeconfig` (or `scripts/k8s-reader-kubeconfig.sh --check` to see why). The
  running kubernetes-mcp picks the new token up on its next call. If it still fails, the
  container predates the directory mount (it mounted the single file, whose atomic replacement
  it never sees): `make mcp-up` recreates it once.
- Code agent: "Scanned 0 commits … touching payment-service" during a live scenario: the
  sample repo was not a healthy S0 one when you injected (e.g. built with `make seed-repo S=S1`
  for the fixtures, so the change was "already in" it), the injection ran with
  `AIOPS_FAULT_GIT_COMMITS=0`, or the host API is older than this feature. Fix: revert, then
  `cd backend && uv run aiops seed repo -S S0`, and inject again. `aiops fault status` lists the
  commits an active fault added.
- Metrics agent says "no baseline data before the window" on every metric: expected on a
  cluster up for less than `baseline_minutes`; anomalies then come from the absolute thresholds.
  Tune them per profile if your SLOs differ.
- Tag: after `make demo` + `make demo-e2e` pass on `main`, the milestone tag is
  `git tag -a v0.6.0 -m "Web UI + make demo" && git push origin v0.6.0` (MASTER_PLAN §14).
