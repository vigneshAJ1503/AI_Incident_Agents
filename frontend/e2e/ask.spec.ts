import { expect, test } from "@playwright/test";

import { expectNoSeriousA11yViolations } from "./helpers";

/** Platform questions (PR-041) in demo mode: answered in the chat, no investigation started. */
test.describe("ask: platform questions (demo mode)", () => {
  test("“what agents are running now?” lists the in-flight run and links to its live view", async ({
    page,
  }) => {
    await page.clock.install();
    await page.goto("/investigations/new");
    await page.getByLabel("Your question").fill("Payment API is returning HTTP 500 in production");
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/investigations\/inv-/);
    const liveUrl = page.url();
    // freeze the demo clock mid-run so the simulated investigation stays in flight
    await page.clock.setFixedTime(Date.now() + 1500);

    await page.keyboard.press("ControlOrMeta+k");
    await page.keyboard.type("what agents are running now?");
    await page.getByRole("option", { name: /^Ask:/ }).click();
    await expect(page).toHaveURL(/\/investigations\/new\?q=/);

    const card = page.getByTestId("answer-card");
    await expect(card).toContainText("1 investigation running");
    await expect(card.getByTestId("running-item")).toContainText(
      "Payment API is returning HTTP 500 in production",
    );
    await card.getByRole("link", { name: "Open live view" }).first().click();
    await expect(page).toHaveURL(liveUrl);
  });

  test("“which agents do you have?” answers with the 7 agents; an unknown incident offers the scenarios", async ({
    page,
  }) => {
    await page.goto("/investigations/new");
    await page.getByRole("button", { name: "Which agents do you have?" }).click();
    const agents = page.getByRole("region", { name: "The agents" });
    await expect(agents.getByTestId("agent-item")).toHaveCount(7);
    await expect(agents).toContainText("prometheus");
    await expect(page).toHaveURL(/\/investigations\/new$/);

    await page.getByLabel("Your question").fill("hello world");
    await page.keyboard.press("Enter");
    const chips = page.getByRole("region", { name: "No recorded incident matches this question" });
    await expect(chips.getByTestId("suggestion-chip")).toHaveCount(5);
    await expectNoSeriousA11yViolations(page);
    await chips.getByRole("button", { name: /Payments are slow in production/ }).click();
    await expect(page).toHaveURL(/\/investigations\/inv-/);
  });

  test("“is the LLM configured?” shows the health checklist", async ({ page }) => {
    await page.goto("/investigations/new");
    await page.getByRole("button", { name: "Is the LLM configured?" }).click();
    const health = page.getByRole("region", { name: "Platform health" });
    await expect(health).toContainText("LLM");
    await expect(health).toContainText("not configured");
  });
});
