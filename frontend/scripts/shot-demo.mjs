/**
 * Screenshot of the REAL product (make demo) for the README: docs/ui/screenshots/make-demo.png.
 *   make demo && node frontend/scripts/shot-demo.mjs
 */
import { chromium } from "@playwright/test";

const base = process.env.E2E_BASE_URL ?? "http://localhost:3100";
const out = new URL("../../docs/ui/screenshots/make-demo.png", import.meta.url).pathname;
const browser = await chromium.launch();
const page = await browser.newPage({
  viewport: { width: 1440, height: 1000 },
  colorScheme: "dark",
});
await page.goto(base);
await page.getByText("Investigations per day").waitFor();
await page.waitForTimeout(1500); // chart entrance animations
await page.screenshot({ path: out });
await browser.close();
process.stdout.write(`wrote ${out}\n`);
