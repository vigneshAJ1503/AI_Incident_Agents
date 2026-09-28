# ADR-0016: One-command demo: replay by default, a same-origin API proxy in the Web UI

- **Status:** Accepted
- **Date:** 2026-09-28
- **PR:** PR-039 (docs/setup/demo.md)

## Context
MASTER_PLAN's golden scenario (`make demo`) was "start everything, inject S1, wait for the
alert, investigate, show the report". Live injection needs Minikube (2.2 GB), the whole data
stack and, for a live investigation, an LLM key the human may not have. The human wants to
*explore the product* from a stopped laptop, quickly and lightly. The Web UI and the API also
live in two containers, and the browser must reach both REST and SSE.

## Decision
1. **`make demo` is replay-first:** Postgres + the API + the Web UI + mock-tickets-mcp
   (≈ 0.3 GB), a seeded 14-day history (`aiops demo seed --if-older-than 12`, run inside the
   API image, so only Docker is needed), and new investigations replayed from the recorded
   fixtures. The live golden scenario moved to **`make demo-live`** (≈ 2.9 GB, faults enabled,
   inject from the Scenarios page). `make demo-down|demo-reset` stop/reseed.
2. **Same-origin proxy:** the web image is built with `NEXT_PUBLIC_API_URL=/api`; a Next.js
   route handler forwards `/api/*` to `AIOPS_API_INTERNAL_URL` (runtime env), streaming the
   body with `Cache-Control: no-cache, no-transform` so SSE isn't buffered. Chosen over a
   `next.config` rewrite (its destination is fixed at build time and SSE through it can be
   compressed/buffered) and over the browser calling `:8000` (CORS, a second port, a URL baked
   into the image).
3. **Paced replays for the UI:** `AIOPS_REPLAY_TOOL_DELAY_S` (compose default 0.5 s, code
   default 0) delays each recorded tool call, so a replay started from the UI animates for
   ~10 s instead of finishing in milliseconds. Tests, evals and seeding stay instant.
4. **In `demo-live` the API runs on the host** (fault injection shells out to `kubectl
   --context aiops`); the web container reaches it via `host.docker.internal`.

## Consequences
- The one command works without Minikube or a key and within ~0.3 GB; the live path is one
  more command and documented.
- Two demo datasets remain (the static UI one and the seed); a unit test keeps them on the
  same story (ground truth + release versions).
- The real-API Playwright suite (`make demo-e2e`) is local, not CI (image builds + seeding).
