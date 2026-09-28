# Web UI (`frontend/`)

Next.js (App Router) + TypeScript (strict) + Tailwind CSS v4 + shadcn-style components on Radix,
Framer Motion, Recharts, TanStack Query and zod. It talks to the API in
[`docs/api/contract.md`](../docs/api/contract.md) or, in demo mode, to a static dataset.

```bash
make ui-install            # npm ci (Node 24 LTS or 26)
make ui-dev DEMO=1         # http://localhost:3100 with the demo dataset, no backend
make ui-dev                # against NEXT_PUBLIC_API_URL (default http://localhost:8000/api)
make ui-test               # ESLint + Prettier, tsc, Vitest
make ui-e2e                # Playwright (chromium) against a demo build, with axe checks
make ui-screenshots        # docs/ui/screenshots/*.png (dark + light)
```

Port 3100 because Grafana owns 3000 locally.

**Container (PR-039):** `frontend/Dockerfile` (Next.js `output: "standalone"` via
`NEXT_OUTPUT=standalone`, `node:24.19.0-alpine3.23`, UID 10001) is the `web` service of
`deploy/compose/docker-compose.app.yml`; `make demo` starts it with the API
([docs/setup/demo.md](../docs/setup/demo.md)). It is built with `NEXT_PUBLIC_API_URL=/api`: the
browser only talks to `:3100`, and `src/app/api/[...path]/route.ts` proxies REST + SSE to
`AIOPS_API_INTERNAL_URL` (runtime). `make demo-e2e` runs `playwright.real.config.ts`
(`e2e/real/`) against it.

## Layout

| Path                         | What                                                                                       |
| ---------------------------- | ------------------------------------------------------------------------------------------ |
| `src/lib/api/schemas.ts`     | zod schemas mirroring the contract; every REST/SSE/demo payload is parsed                  |
| `src/lib/api/http-client.ts` | `HttpClient`: REST + SSE (EventSource, backoff reconnect, resume by seq, de-dup)           |
| `src/lib/api/demo-client.ts` | `DemoClient`: serves `src/demo/*.json`, replays recorded SSE streams in real time          |
| `src/lib/api/index.ts`       | `getClient()`: `NEXT_PUBLIC_DEMO=1` selects the demo client                                |
| `src/lib/queries.ts`         | TanStack Query hooks (optimistic approvals)                                                |
| `src/demo/`                  | the demo dataset ([README](src/demo/README.md)); `scripts/generate-demo.ts` regenerates it |
| `src/components/`            | layout (sidebar, ⌘K palette, shortcuts), status badges, charts                             |
| `e2e/`                       | Playwright specs (demo mode)                                                               |

## Visual design (glass)

Tokens live in `src/app/globals.css` (light + dark):

- **Surfaces:** `glass` (cards: a _tinted_ translucent fill + 1px border + inner highlight +
  `--elev-2`) and `glass-chrome` (topbar, dialogs, drawers, ⌘K, the sticky chat composer and report
  tab bar: `backdrop-filter` blur). Only floating chrome blurs, and only small areas: cards sit on
  the already-soft aurora, so a blur there would cost paint time for no visual gain. Without
  `backdrop-filter` support, `glass-chrome` falls back to an opaque tinted fill.
- **Elevation:** `--elev-1..4` (`shadow-elev-*`); `lift` = hover lift + glow for interactive cards.
- **Aurora:** one fixed, composited layer (`.aurora`) animating `transform` only; frozen under
  `prefers-reduced-motion`.
- **Density:** `html[data-density=compact]` (topbar toggle, per browser) sets `--pad`/`--row-y`.
- Text on glass keeps WCAG AA (axe runs in every e2e spec).

Visual regression: `e2e/visual.spec.ts` (`toHaveScreenshot`, dashboard/report/chat × light/dark,
1.5% tolerance). Baselines are Linux-only (CI); `make ui-visual-update` regenerates them in the
pinned Playwright container.

## Configuration

See [`.env.example`](.env.example). `NEXT_PUBLIC_*` values are inlined at build time.

## Quality bar

`next build` without warnings, no `any`, typed routes, an error boundary per route with retry,
`prefers-reduced-motion` respected (Framer `MotionConfig reducedMotion="user"` + CSS), colour-blind
safe status colours (Okabe-Ito) always paired with an icon and a label, axe checks (no serious
violations) in e2e.
