# ADR-0015: API: in-process background investigations, progressive persistence, SSE replay-then-follow

- **Status:** Accepted
- **Date:** 2026-09-28
- **PR:** PR-035 (docs/api/contract.md, docs/api/README.md)

## Context
The Web UI (PR-036..039) needs to start investigations, watch them live and reconnect without
losing events, on a laptop with a 5 GB Docker budget, zero cost and usually **no LLM key**.
The orchestrator (ADR-0014) already publishes the contract's events on an in-process
`EventBus` with a per-investigation `seq`; the evidence store (PR-032) persists
investigations and their event log.

## Decision
1. **Background asyncio tasks in the API process** (no queue, no worker): `POST
   /investigations` saves a `pending` investigation and returns 202; the orchestrator runs as
   a task. A per-process limit (`api.max_running_investigations`, default 2) answers 429
   beyond it. Cancellation uses `Orchestrator.cancel` (new status `cancelled`).
2. **Progressive persistence:** the runner subscribes to the bus and flushes events to the
   store every 250 ms, with a snapshot of the live investigation after plan / agent / RCA
   events; the final investigation is saved when the run ends. A flush never gets cancelled
   mid-write (stop event, not `task.cancel`).
3. **SSE = replay, then follow:** subscribe to the bus first, replay the stored events after
   `Last-Event-ID` (or `?last_event_id=`: `EventSource` can't set headers), then follow the
   bus (dedup by `seq`); if the investigation runs elsewhere, poll the store. A `heartbeat`
   every 15 s carries the last `seq` and is never stored. The stream ends after
   `investigation_finished`. A clarification re-runs the investigation with the same id and
   restores the bus history, so `seq` continues on the same stream.
4. **Mode:** `live` when an LLM key and model are configured and every enabled capability's
   MCP server accepts a TCP connection (cached 30 s: `aiops doctor` stays the thorough
   check); else `replay` of the scenario named (`scenario`) or matched by question/service
   (fixtures exist only for a scenario's own service). Otherwise a clear 409/422.
5. **Approvals** reuse the framework unchanged: drafts go through `ApprovalService.propose`,
   approve executes through `ApprovalExecutor` (the only write path). The UI's approval
   states are a subset: `rejected`/`expired` are reported as `denied` with the exact value
   in `lifecycle_status`.
6. **Fault endpoints** are opt-in (`AIOPS_ENABLE_FAULTS=1`, else 403) and reuse the injector,
   the cluster lock and the kubectl context; revert runs in a thread (rollouts take minutes).

## Consequences
- One process is the unit of scale; several API processes still share the store (SSE polls
  it), but the concurrency limit is per process. A crash leaves a `running` investigation
  that can be closed with `/cancel`.
- No extra infrastructure (Redis/queue) and ~120 MiB for the API container.
- Replays make the whole UI demo-able with zero tokens and no key.
