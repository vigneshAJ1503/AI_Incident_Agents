import { expect, test, type Page } from "@playwright/test";

import { expectNoSeriousA11yViolations } from "./helpers";

/**
 * Live-demo regressions on the report: a PARTIAL run with abnormal signals was headed "No incident
 * detected" in green, and old OPS tickets topped the "Why it happened" timeline. The demo
 * dataset's partial run (S2 with the Metrics and Kubernetes agents failed, old tickets stored
 * first) covers both: src/lib/demo/partial.ts.
 */
const PARTIAL = "/investigations/inv-5be7c43f7";

async function open(page: Page, path: string, theme: "light" | "dark") {
  await page.emulateMedia({ colorScheme: theme });
  await page.goto(path);
  await page.evaluate((t) => localStorage.setItem("theme", t), theme);
  await page.reload();
  await expect(page.locator("html")).toHaveClass(new RegExp(theme));
}

for (const theme of ["light", "dark"] as const) {
  test.describe(`${theme} theme`, () => {
    test("partial run with signals: 'No root cause identified' with a weak lead", async ({
      page,
    }) => {
      await open(page, PARTIAL, theme);
      const hero = page.getByTestId("root-cause");
      await expect(hero).toHaveAttribute("data-outcome", "no_root_cause");
      await expect(hero).toContainText("No root cause identified");
      await expect(hero).not.toContainText("No incident detected");
      await expect(hero).toContainText("High");
      await expect(hero.getByTestId("weak-lead")).toContainText("Weak lead · not confirmed");
      await expect(hero.getByTestId("weak-lead")).toContainText(/Memory leak in order-service/);
      await expect(hero.getByTestId("no-root-cause-hint")).toContainText(/re-run/);
      await expect(page.getByTestId("confidence")).toHaveCount(0);

      // the incident's own chain first; the old tickets collapsed under one row after it
      const timeline = page.getByTestId("timeline");
      await expect(timeline.getByRole("button").first()).toContainText(/v2\.3\.0 rolled out/);
      await expect(timeline).not.toContainText("OPS-17");
      const related = page.getByRole("button", { name: /Related tickets \(4\)/ });
      await expect(related).toHaveAttribute("aria-expanded", "false");
      await expectNoSeriousA11yViolations(page);
      await related.click();
      await expect(related).toHaveAttribute("aria-expanded", "true");
      await expect(page.getByTestId("timeline-related")).toContainText("OPS-17");
      await expectNoSeriousA11yViolations(page);
    });

    test("S0 stays a true 'No incident detected'", async ({ page }) => {
      await open(page, "/investigations/inv-demo-s0", theme);
      const hero = page.getByTestId("root-cause");
      await expect(hero).toHaveAttribute("data-outcome", "healthy");
      await expect(hero).toContainText("No incident detected");
      await expect(hero.getByTestId("weak-lead")).toHaveCount(0);
      await expectNoSeriousA11yViolations(page);
    });

    test("scenarios page renders without a11y violations", async ({ page }) => {
      await open(page, "/scenarios", theme);
      await expect(page.getByRole("button", { name: "Revert all" })).toBeDisabled();
      await expect(page.getByTestId("scenario-S1")).not.toContainText("active");
      await expectNoSeriousA11yViolations(page);
    });
  });
}
