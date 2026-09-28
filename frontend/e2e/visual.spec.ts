import { expect, test, type Page } from "@playwright/test";

/**
 * Visual regression (dashboard, report, chat; light + dark). Font rendering differs per OS, so the
 * baselines are Linux-only: CI (ubuntu) compares them, and `make ui-visual-update` regenerates them
 * inside the pinned Playwright container. Elsewhere these tests skip unless VISUAL=1.
 */
test.skip(
  process.platform !== "linux" && !process.env.VISUAL,
  "visual baselines are Linux-only (make ui-visual-update)",
);
test.use({ viewport: { width: 1280, height: 860 } });

// the demo dataset is anchored to "today": freeze the clock so dates and "x hours ago" are stable
const NOW = new Date("2026-09-29T12:00:00Z");

async function open(page: Page, path: string, theme: "light" | "dark") {
  await page.clock.setFixedTime(NOW);
  await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
  await page.goto(path);
  await page.evaluate((t) => localStorage.setItem("theme", t), theme);
  await page.reload();
  await page.waitForLoadState("networkidle");
}

for (const theme of ["light", "dark"] as const) {
  test.describe(`${theme} theme`, () => {
    test(`dashboard`, async ({ page }) => {
      await open(page, "/", theme);
      await expect(page.locator(".recharts-surface").first()).toBeVisible();
      await expect(page).toHaveScreenshot(`dashboard-${theme}.png`);
    });

    test(`report`, async ({ page }) => {
      await open(page, "/investigations/inv-demo-s1", theme);
      await expect(page.getByTestId("confidence")).toHaveText("91%");
      await expect(page).toHaveScreenshot(`report-${theme}.png`);
    });

    test(`chat reply`, async ({ page }) => {
      await open(page, "/investigations/new", theme);
      await page.getByRole("button", { name: "Which agents do you have?" }).click();
      await expect(page.getByTestId("answer-card")).toBeVisible();
      await page.getByLabel("Your question").blur();
      await expect(page).toHaveScreenshot(`chat-${theme}.png`);
    });
  });
}
