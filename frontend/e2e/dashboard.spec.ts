import { expect, test } from "@playwright/test";

import { expectNoSeriousA11yViolations } from "./helpers";

test.describe("dashboard (demo mode)", () => {
  test("renders KPIs, charts, status strip and recent investigations", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    await expect(page.getByTestId("mode-banner")).toContainText("Demo mode");
    await expect(page.getByRole("region", { name: "System status" })).toContainText(
      "LLM not configured",
    );
    await expect(page.getByText("Investigations per day")).toBeVisible();
    await expect(page.locator(".recharts-surface").first()).toBeVisible();
    await expect(page.getByText("Agent performance")).toBeVisible();
    await expect(
      page.getByRole("link", { name: /Payment API is returning HTTP 500/ }).first(),
    ).toBeVisible();
    await expectNoSeriousA11yViolations(page);
  });

  test("theme toggle switches between light and dark", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto("/");
    const html = page.locator("html");
    await expect(html).not.toHaveClass(/dark/);
    await page.getByTestId("theme-toggle").click();
    await expect(html).toHaveClass(/dark/);
    await expectNoSeriousA11yViolations(page);
    await page.getByTestId("theme-toggle").click();
    await expect(html).not.toHaveClass(/dark/);
  });

  test("keyboard shortcuts and the command palette navigate", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    await page.keyboard.press("g");
    await page.keyboard.press("i");
    await expect(page).toHaveURL(/\/investigations$/);
    await page.keyboard.press("ControlOrMeta+k");
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.type("Dashboard");
    await page.getByRole("option", { name: /^Dashboard/ }).click();
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("dialog")).toBeHidden();
    await page.keyboard.press("?");
    await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
  });
});

test.describe("investigations list", () => {
  test("filters, searches and opens an investigation", async ({ page }) => {
    await page.goto("/investigations");
    const rows = page.locator("tbody tr");
    await expect(rows.first()).toBeVisible();
    await page.getByLabel("Filter by status").selectOption("failed");
    await expect(rows).toHaveCount(2);
    await page.getByLabel("Filter by status").selectOption("");
    await page.getByLabel("Search investigations").fill("inventory");
    await expect(rows.first()).toContainText(/order/i);
    await page.getByLabel("Search investigations").fill("zzzz-nothing");
    await expect(page.getByText("No investigations match these filters")).toBeVisible();
    await page.getByRole("button", { name: "Clear" }).click();
    await expect(rows.first()).toBeVisible();
    await expectNoSeriousA11yViolations(page);
  });
});
