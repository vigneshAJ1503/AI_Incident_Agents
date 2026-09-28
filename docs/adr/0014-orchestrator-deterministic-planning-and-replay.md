# ADR-0014: Orchestrator: deterministic planning, rule-based gap analysis, whole-investigation replay

- **Status:** accepted
- **Date:** 2026-09-28
- **PRs:** PR-030/031 (planner, executor), extended by PR-032 (store) and PR-033/034 (RCA, report)

## Context

Seven specialist agents work standalone. PR-030..034 turn them into ONE investigation
(MASTER_PLAN §12, UC-10, UC-13). Constraints: zero cost (free-tier LLMs, tests with zero
tokens), portability by configuration only (ADR-0011/0012), evidence-first.

## Decision

1. **Planner: rules first, LLM only as a fallback.** Service = catalog names/aliases found in
   the question (never invented; an LLM suggestion must also resolve through the catalog).
   Several services -> the caller when the others are its dependencies, else
   `needs_clarification` with candidates. Time ranges and symptoms are parsed with regexes.
   The `fast` model is called only when no service was found, and never in replay.
2. **The plan is built from the registry + the profile**: every registered agent whose
   capabilities are enabled. `orchestrator.round2_agents` (default `knowledge`, `tickets`)
   run in round 2 because they need round-1 findings (knowledge gets `hints.signals /
   patterns / alerts / keywords`; tickets get round-1 signals as `context.symptoms`).
   Everything else (code and k8s included) runs in round 1.
3. **Gap analysis is a deterministic table** `signal -> follow-ups` (defaults in
   `orchestrator/gaps.py`, extended/overridden by `orchestrator.followups`). A follow-up on
   the incident service is deduplicated when round 1 already ran that agent successfully (no
   duplicate work, no duplicate tokens); `target: dependency` runs the agent on the catalog
   dependency named by round-1 evidence (e.g. "Timeout calling inventory-service").
4. **Agents publish structured hints in evidence data** (`patterns` from logs, `alerts` from
   alerts) and set evidence timestamps, so the orchestrator never parses agent prose.
5. **Executor**: asyncio + semaphore, per-step timeout (agent limit + 15 s), cancellation, a
   token budget per investigation; any failed/partial/skipped step makes the investigation
   PARTIAL, never a crash. Task id == step id, so agent events map onto steps.
6. **Events**: an in-process `EventBus` publishing exactly the contract's SSE event types,
   numbered `seq` per investigation, with history for replay (`Last-Event-ID`).
7. **Whole-investigation replay** reuses the agents' recorded fixtures. Each agent runs with
   the window its fixtures were recorded with (`meta.json`); live-recorded results are then
   shifted onto the scenario clock (incident start = `REPLAY_NOW - 20m`), so the timeline and
   the RCA's time alignment see one coherent clock. Round-2 queries built from real hints
   differ from the standalone recordings, so replay is **lenient** there: an unrecorded call
   is served by the most similar recorded call of the same tool. Follow-ups on another
   service have no fixtures and are skipped (a recoverable `error` event).
8. **No LLM configured** (no key): agents submit their deterministic overview (the replay
   responder), and the planner/RCA stay rule-based. The system works end to end with zero
   tokens; the LLM only adds phrasing and ranking.

## Consequences

- Investigations are deterministic and free in CI; the LLM is an optional enhancement.
- Lenient replay trades fidelity for coverage: a replayed knowledge/tickets step may serve a
  recorded response to a slightly different query. It is only enabled for orchestrated
  replays, never for the agents' own evals.
- New signals need a gap rule to get follow-ups; unknown signals are simply passed on as
  hints/symptoms.
