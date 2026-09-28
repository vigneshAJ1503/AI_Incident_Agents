import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

/** Fail on serious/critical axe violations (WCAG 2.1 A/AA). */
export async function expectNoSeriousA11yViolations(page: Page) {
  // let entrance animations (opacity fades) settle so contrast is measured on final colours
  await page.waitForTimeout(900);
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const serious = results.violations.filter(
    (v) => v.impact === "serious" || v.impact === "critical",
  );
  const summary = serious.map(
    (v) =>
      `${v.id} (${v.impact}): ${v.help} → ${v.nodes
        .map((n) => n.target.join(" "))
        .slice(0, 3)
        .join(" | ")}`,
  );
  expect(summary, summary.join("\n")).toEqual([]);
}
