import { defineConfig, devices } from "@playwright/test";

/**
 * E2E against the REAL product (PR-039): the web + api containers of `make demo` with the
 * seeded Postgres history and mock-tickets-mcp. No web server is started here.
 *
 *   make demo && make demo-e2e      (E2E_BASE_URL defaults to http://localhost:3100)
 *
 * The demo-mode suite (playwright.config.ts) stays the CI one; see docs/setup/demo.md.
 */
export default defineConfig({
  testDir: "./e2e/real",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  timeout: 90_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3100",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "real-api", use: { ...devices["Desktop Chrome"] } }],
});
