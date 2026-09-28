import { test, type Page } from "@playwright/test";

/**
 * Captures the README/PR screenshots into docs/ui/screenshots (dark + light). Skipped unless
 * SCREENSHOTS=1:  SCREENSHOTS=1 npx playwright test screenshots
 */
test.skip(!process.env.SCREENSHOTS, "set SCREENSHOTS=1 to capture docs screenshots");
test.use({ viewport: { width: 1440, height: 900 } });

const OUT = "../docs/ui/screenshots";

async function shoot(page: Page, name: string, theme: "light" | "dark") {
  await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
  await page.evaluate((t) => localStorage.setItem("theme", t), theme);
  await page.reload();
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${OUT}/${name}-${theme}.png`, fullPage: true });
}

const PAGES: [string, string][] = (
  process.env.SCREENSHOT_PAGES ?? "dashboard=/,investigations=/investigations"
)
  .split(",")
  .map((p) => p.split("=") as [string, string]);

for (const [name, path] of PAGES) {
  for (const theme of ["light", "dark"] as const) {
    test(`${name} (${theme})`, async ({ page }) => {
      await page.goto(path);
      await shoot(page, name, theme);
    });
  }
}
