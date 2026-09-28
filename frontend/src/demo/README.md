# Web UI demo dataset

Static, contract-shaped JSON that lets the UI run with **no backend** (`NEXT_PUBLIC_DEMO=1`, or
`make ui-dev DEMO=1`). Every file is validated against the zod schemas in
`src/lib/api/schemas.ts` (which mirror `docs/api/contract.md`) when it is generated _and_ when the
UI loads it.

| File                   | Contract resource                      | Notes                                                                                                                                                         |
| ---------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `health.json`          | `GET /health`                          | `llm.configured: false`, all capabilities `ok`, `faults_enabled: false`                                                                                       |
| `services.json`        | `GET /services`                        | the 4 sample services from `profiles/local/services.yaml`                                                                                                     |
| `agents.json`          | `GET /agents`                          | the 7 specialist agents + `rca`                                                                                                                               |
| `dashboard.json`       | `GET /dashboard/summary?days=14`       | computed from `investigations.json`                                                                                                                           |
| `investigations.json`  | `GET /investigations`                  | `{items: InvestigationSummary[], scenario_of: {id: "S1"}}`: 40 investigations over 14 days; `scenario_of` maps each one to the recording its detail view uses |
| `scenario-S0..S5.json` | `GET /investigations/{id}` + `/events` | `{investigation, events}`: a full investigation and its recorded SSE stream                                                                                   |
| `approvals.json`       | `GET /approvals`                       | one **pending** Jira draft (S1), one executed, one denied                                                                                                     |
| `scenarios.json`       | `GET /scenarios`                       | S1–S5 (inject/revert are refused in demo mode)                                                                                                                |
| `meta.json`            | –                                      | `anchor`: the dataset's "now" (see time shifting)                                                                                                             |

## How the demo client uses it (`src/lib/api/demo-client.ts`)

- **Time shifting.** Timestamps are moved forward by whole days so the newest item is always in
  the recent past (`anchor` in `meta.json`); dates in the charts move with them.
- **Simulated live runs.** A new investigation is routed to a scenario by its question
  (`src/lib/demo/route.ts`: "Payment API is returning HTTP 500" → S1, "Something is broken" →
  clarification) and the scenario's recorded events are replayed on a real-time schedule, sped up
  by `NEXT_PUBLIC_DEMO_SPEED` (default 4×). Reloading the page replays past events first, like the
  real SSE endpoint. Clarify, cancel, ticket drafts and approvals work and live in
  `sessionStorage`.

## Regenerating

```bash
make ui-demo-data        # = cd frontend && npm run demo:generate
```

`scripts/generate-demo.ts` is deterministic (fixed seed and calendar), so CI checks that the
committed files are up to date. The numbers come from the recorded replay fixtures
(`backend/tests/fixtures/<agent>/S*`) and `scenarios/S*/`: e.g. S1's 44 `Database connection
timeout` errors, `DB_POOL_SIZE: "20" → "2"` in commit `739d116`, 5xx 0.5% → 24.6%, OPS-12.

**Later:** `aiops demo export` (PR-034) can overwrite these files from real replay runs. Keep the
layout above; anything else the UI needs is derived at runtime.
