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

## Configuration

See [`.env.example`](.env.example). `NEXT_PUBLIC_*` values are inlined at build time.

## Quality bar

`next build` without warnings, no `any`, typed routes, an error boundary per route with retry,
`prefers-reduced-motion` respected (Framer `MotionConfig reducedMotion="user"` + CSS), colour-blind
safe status colours (Okabe-Ito) always paired with an icon and a label, axe checks (no serious
violations) in e2e.
