import { expect, test, type Page } from "@playwright/test";

/**
 * Captures the README/PR screenshots into docs/ui/screenshots (dark + light). Skipped unless
 * SCREENSHOTS=1 (`make ui-screenshots`). SCREENSHOT_PAGES narrows the static pages.
 */
test.skip(!process.env.SCREENSHOTS, "set SCREENSHOTS=1 to capture docs screenshots");
test.use({ viewport: { width: 1440, height: 900 } });

const OUT = "../docs/ui/screenshots";
const THEMES = ["light", "dark"] as const;

async function setTheme(page: Page, theme: "light" | "dark") {
  await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
  await page.evaluate((t) => localStorage.setItem("theme", t), theme);
}

const PAGES: [string, string][] = (
  process.env.SCREENSHOT_PAGES ??
  "dashboard=/,investigations=/investigations,new-investigation=/investigations/new,report=/investigations/inv-demo-s1,approvals=/approvals,scenarios=/scenarios,agents=/agents,services=/services"
)
  .split(",")
  .map((p) => p.split("=") as [string, string]);

for (const [name, path] of PAGES) {
  for (const theme of THEMES) {
    test(`${name} (${theme})`, async ({ page }) => {
      const res = await page.goto(path);
      test.skip(res?.status() === 404, `${path} doesn't exist in this build`);
      await setTheme(page, theme);
      await page.reload();
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(800);
      await page.screenshot({ path: `${OUT}/${name}-${theme}.png`, fullPage: true });
    });
  }
}

for (const theme of THEMES) {
  test(`chat reply card (${theme})`, async ({ page }) => {
    await page.goto("/investigations/new");
    await setTheme(page, theme);
    await page.reload();
    await page.getByRole("button", { name: "Which agents do you have?" }).click();
    await expect(page.getByTestId("answer-card")).toBeVisible();
    await page.waitForTimeout(600);
    await page.screenshot({ path: `${OUT}/chat-reply-${theme}.png`, fullPage: true });
  });

  test(`live run (${theme})`, async ({ page }) => {
    test.skip(
      !process.env.SCREENSHOT_LIVE,
      "set SCREENSHOT_LIVE=1 (use a slow NEXT_PUBLIC_DEMO_SPEED)",
    );
    await page.goto("/investigations/new");
    await setTheme(page, theme);
    await page.reload();
    await page.getByLabel("Your question").fill("Payment API is returning HTTP 500 in production");
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("lane-alerts-r1")).toHaveAttribute("data-phase", "done", {
      timeout: 30_000,
    });
    await page.screenshot({ path: `${OUT}/live-${theme}.png`, fullPage: true });
  });
}
