import { expect, test, type Page } from "@playwright/test";

import { expectNoSeriousA11yViolations } from "./helpers";

// built at runtime: never a real credential
const SECRET = `Bearer e2e-${Date.now().toString(36)}-${"q".repeat(10)}-k9z4`;

async function openIntegrations(page: Page) {
  await page.goto("/settings/integrations");
  await expect(page.getByRole("heading", { name: "Integrations", level: 1 })).toBeVisible();
}

test.describe("settings → integrations (demo mode)", () => {
  test("one card per capability, status, provider and the Slack placeholder", async ({ page }) => {
    await openIntegrations(page);
    for (const cap of ["logs", "metrics", "alerts", "k8s", "code", "tickets", "knowledge"]) {
      await expect(page.getByTestId(`integration-${cap}`)).toBeVisible();
    }
    await expect(page.getByTestId("integration-logs")).toContainText("elasticsearch");
    await expect(page.getByTestId("integration-logs")).toContainText("Connected");
    await expect(page.getByTestId("integration-slack")).toContainText("Coming soon");
    // a secret saved earlier is shown masked: last 4 characters at most
    await expect(page.getByTestId("secret-tickets-Authorization")).toHaveText(
      "Authorization: ••••9f2c",
    );
    // reachable from the sidebar
    await expect(page.getByRole("link", { name: "Integrations" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  test("edit, test connection, save, and the secret stays masked", async ({ page }) => {
    await openIntegrations(page);
    await page.getByRole("button", { name: "Configure Logs" }).click();
    const dialog = page.getByTestId("integration-dialog");
    await expect(dialog.getByRole("heading", { name: /Configure Logs/ })).toBeVisible();
    const save = dialog.getByRole("button", { name: "Save" });
    await expect(save).toBeDisabled();

    // edit a field mapping (portability: the company's log schema)
    const level = dialog.getByLabel("fields.level");
    await level.fill("log.level");
    await expect(save).toBeEnabled();

    // test the draft: an unreachable server fails with the doctor's reachability row ...
    const url = dialog.getByLabel("MCP server URL");
    const original = await url.inputValue();
    await url.fill("http://127.0.0.1:9/mcp");
    await dialog.getByRole("button", { name: "Test connection" }).click();
    const results = dialog.getByTestId("test-results");
    await expect(results).toContainText("Test fail");
    await expect(results).toContainText("reachability");
    await expect(results).toContainText("cannot connect to http://127.0.0.1:9/mcp");

    // ... and passes once it points back at the real one
    await url.fill(original);
    await dialog.getByRole("button", { name: "Test connection" }).click();
    await expect(results).toContainText("Test pass");
    await expect(results).toContainText("smoke");

    // a secret is typed into a password field and never shown again
    const secret = dialog.getByLabel("Secret value");
    await expect(secret).toHaveAttribute("type", "password");
    await secret.fill(SECRET);
    await dialog.getByRole("button", { name: "Set secret" }).click();
    await expect(dialog).toContainText("Authorization: new value (unsaved)");
    await save.click();

    await expect(page.getByText("Logs saved")).toBeVisible();
    await expect(dialog).toBeHidden();
    const card = page.getByTestId("integration-logs");
    await expect(page.getByTestId("secret-logs-Authorization")).toHaveText(
      `Authorization: ••••${SECRET.slice(-4)}`,
    );
    await expect(card).toContainText("2 UI overrides · demo-operator");
    await expect(page.locator("body")).not.toContainText(SECRET);
    await expect(page.locator("body")).not.toContainText(SECRET.slice(0, -4));

    // reopening shows the override, the profile default and the masked secret
    await page.getByRole("button", { name: "Configure Logs" }).click();
    await expect(dialog.getByLabel("fields.level")).toHaveValue("log.level");
    await expect(dialog).toContainText("profile: level");
    await expect(dialog).toContainText("UI override");
    await expect(dialog).toContainText(`Authorization: ••••${SECRET.slice(-4)}`);
    await expect(dialog.getByLabel("Secret value")).toHaveValue("");
    await expect(page.locator("body")).not.toContainText(SECRET);
  });

  for (const theme of ["light", "dark"] as const) {
    test(`is accessible in the ${theme} theme (page and dialog)`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: theme });
      await openIntegrations(page);
      await expect(page.locator("html")).toHaveClass(theme === "dark" ? /dark/ : /^(?!.*dark)/);
      await expectNoSeriousA11yViolations(page);
      await page.getByRole("button", { name: "Configure Metrics" }).click();
      await expect(page.getByTestId("integration-dialog")).toBeVisible();
      await page.getByRole("button", { name: "Test connection" }).click();
      await expect(page.getByTestId("test-results")).toContainText("Test pass");
      await expectNoSeriousA11yViolations(page);
    });
  }
});
