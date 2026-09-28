import { expect, test } from "@playwright/test";

import { expectNoSeriousA11yViolations } from "./helpers";

test.describe("live investigation (demo mode)", () => {
  test("S1: ask → agents animate → report", async ({ page }) => {
    await page.goto("/investigations/new");
    await expect(page.getByRole("heading", { name: "What's going wrong?" })).toBeVisible();
    await expectNoSeriousA11yViolations(page);
    await page.getByLabel("Your question").fill("Payment API is returning HTTP 500 in production");
    await page.keyboard.press("Enter");

    await expect(page).toHaveURL(/\/investigations\/inv-/);
    const live = page.getByTestId("live-view");
    await expect(live).toBeVisible();
    // agents go queued/running → done
    const logs = page.getByTestId("lane-logs-r1");
    await expect(logs).toBeVisible();
    await expect(logs).toHaveAttribute("data-phase", /running|done/);
    await expect(page.getByRole("log", { name: "Investigation event log" })).toContainText(
      "Round 1",
    );
    // the event log follows new events until paused
    await page.getByTestId("log-pause").click();
    await expect(page.getByTestId("log-pause")).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("log-unseen")).toBeVisible({ timeout: 15_000 });
    await page.getByTestId("log-unseen").click();
    await expect(page.getByTestId("log-pause")).toHaveAttribute("aria-pressed", "false");
    await expect(logs).toHaveAttribute("data-phase", "done", { timeout: 30_000 });
    await expect(page.getByTestId("lane-logs-r2")).toBeVisible({ timeout: 30_000 });
    // then the report replaces the live view
    await expect(page.getByTestId("report")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId("root-cause")).toContainText(/DB_POOL_SIZE|connection pool/i);
    await expect(page.getByTestId("confidence")).toHaveText("91%");
  });

  test("clarification: 'Something is broken' asks for a service, then resumes", async ({
    page,
  }) => {
    await page.goto("/investigations/new");
    await page.getByRole("button", { name: /Something is broken/ }).click();
    const prompt = page.getByTestId("clarification");
    await expect(prompt).toBeVisible();
    await expectNoSeriousA11yViolations(page);
    await prompt.getByRole("button", { name: "payment-service" }).click();
    await expect(prompt).toBeHidden();
    await expect(page.getByTestId("lane-metrics-r1")).toBeVisible({ timeout: 15_000 });
  });

  test("cancel stops a running investigation", async ({ page }) => {
    await page.goto("/investigations/new");
    await page.getByRole("button", { name: /Orders are failing intermittently/ }).click();
    await expect(page.getByTestId("live-view")).toBeVisible();
    await page.getByTestId("cancel").click();
    await expect(page.getByTestId("live-view")).toHaveAttribute("data-phase", "cancelled", {
      timeout: 10_000,
    });
  });
});
