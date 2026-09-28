import { defineConfig, devices } from "@playwright/test";

/**
 * E2E runs against a production build in demo mode (no backend):
 *   NEXT_PUBLIC_DEMO=1 NEXT_PUBLIC_DEMO_SPEED=12 npm run build && npm run e2e
 * `make ui-e2e` does both.
 */
const PORT = Number(process.env.E2E_PORT ?? 3107);

export default defineConfig({
  testDir: "./e2e",
  testIgnore: ["real/**"], // the real-API suite: playwright.real.config.ts (make demo-e2e)
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : 3,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : [["list"]],
  timeout: 45_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `npx next start --hostname 127.0.0.1 --port ${PORT}`,
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { NEXT_TELEMETRY_DISABLED: "1" },
  },
});
