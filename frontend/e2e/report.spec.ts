import { expect, test } from "@playwright/test";

import { expectNoSeriousA11yViolations } from "./helpers";

test.describe("report view (S1)", () => {
  test("root cause, confidence, evidence drawer, tabs and contradicting evidence", async ({
    page,
  }) => {
    await page.goto("/investigations/inv-demo-s1");
    const hero = page.getByTestId("root-cause");
    await expect(hero).toContainText("Root cause identified");
    await expect(hero).toContainText(/DB_POOL_SIZE 20 → 2/);
    await expect(page.getByTestId("confidence")).toHaveText("91%");
    await expectNoSeriousA11yViolations(page);

    // clickable citation → drawer with the evidence and a deep link
    await hero.getByTestId("citation").filter({ hasText: "ev-s1-logs-1" }).click();
    const drawer = page.getByTestId("evidence-drawer");
    await expect(drawer).toBeVisible();
    await expect(drawer).toContainText("Database connection timeout");
    await expect(drawer.getByRole("link", { name: /View in Kibana/ })).toHaveAttribute(
      "href",
      /localhost:5601/,
    );
    await page.keyboard.press("Escape");
    await expect(drawer).toBeHidden();

    // typed claims and contradicting evidence
    await expect(page.getByTestId("finding").first()).toContainText("FACT");
    await expect(page.getByTestId("contradicting").first()).toBeVisible();

    // timeline click opens the evidence
    await page.getByRole("button", { name: /First 'Database connection timeout' error/ }).click();
    await expect(page.getByTestId("evidence-drawer")).toContainText("ev-s1-logs-1");
    await page.keyboard.press("Escape");

    // metrics tab: chart with the anomaly window; changes tab: the diff hunk
    await page.getByRole("tab", { name: /Metrics/ }).click();
    await expect(page.locator(".recharts-reference-area").first()).toBeVisible();
    await page.getByRole("tab", { name: /Changes/ }).click();
    await expect(page.getByLabel("Diff").first()).toContainText('DB_POOL_SIZE: "2"');
    await page.getByRole("tab", { name: /Runbooks/ }).click();
    await expect(page.getByRole("link", { name: /Open runbook/ }).first()).toBeVisible();
    await expectNoSeriousA11yViolations(page);
  });

  test("S0 shows 'no incident detected' without a hallucinated root cause", async ({ page }) => {
    await page.goto("/investigations/inv-demo-s0");
    await expect(page.getByTestId("root-cause")).toContainText("No incident detected");
    await expect(page.getByTestId("confidence")).toHaveCount(0);
  });

  test("Create Jira ticket → approval dialog → approve", async ({ page }) => {
    await page.goto("/investigations/inv-demo-s1");
    await page.getByTestId("create-jira").click();
    const dialog = page.getByRole("dialog", { name: "Create a Jira ticket" });
    await expect(dialog).toContainText("jira_create_issue");
    await dialog.getByRole("button", { name: "Approve" }).click();
    await page.getByTestId("confirm-decision").click();
    await expect(page.getByText(/Created OPS-\d+/)).toBeVisible();
  });
});

test.describe("approvals", () => {
  test("approve the pending Jira draft with confirmation, toast and history", async ({ page }) => {
    await page.goto("/approvals");
    await expect(page.getByRole("heading", { name: "Approvals" })).toBeVisible();
    const pending = page.getByTestId("approval-card").first();
    await expect(pending).toContainText("pending");
    await expectNoSeriousA11yViolations(page);
    await pending.getByRole("button", { name: "Approve" }).click();
    await expect(page.getByRole("dialog", { name: "Approve and execute?" })).toBeVisible();
    await page.getByTestId("confirm-decision").click();
    await expect(page.getByText(/Created OPS-\d+/)).toBeVisible();
    await expect(page.getByText("Nothing waiting for approval")).toBeVisible();
  });
});

test.describe("catalog pages", () => {
  for (const [path, heading] of [
    ["/services", "Services"],
    ["/agents", "Agents"],
    ["/scenarios", "Scenarios"],
  ] as const) {
    test(`${heading} renders without a11y violations`, async ({ page }) => {
      await page.goto(path);
      await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();
      await expect(page.locator("main [data-slot=card]").first()).toBeVisible();
      await expectNoSeriousA11yViolations(page);
    });
  }

  test("scenarios: inject is disabled in demo mode; run demo investigation replays S5", async ({
    page,
  }) => {
    await page.goto("/scenarios");
    const s5 = page.getByTestId("scenario-S5");
    await expect(s5.getByRole("button", { name: "Inject" })).toBeDisabled();
    await s5.getByRole("button", { name: "Run demo investigation" }).click();
    await expect(page).toHaveURL(/\/investigations\/inv-/);
    await expect(page.getByTestId("live-view")).toBeVisible();
  });
});
