import { expect, test, type APIRequestContext } from "@playwright/test";

/**
 * Settings → Integrations against the real stack (PR-046): the API container, Postgres
 * (overrides + audit tables) and the live mock-tickets MCP server. Uses the `tickets`
 * capability, the one that is reachable in `make demo`, and always resets it to the
 * profile afterwards so the other suites' Jira flow keeps working.
 * Needs AIOPS_SECRETS_KEY set for the API (secrets are refused without it).
 */

const SECRET = `Bearer e2e-${Date.now()}-s3cr3tZ9`;

async function reset(request: APIRequestContext) {
  const res = await request.put("/api/integrations/tickets", { data: { reset: true } });
  expect(res.ok()).toBeTruthy();
}

test.afterEach(async ({ request }) => reset(request));

test("lists every capability; tickets is live and secrets can be saved", async ({ request }) => {
  const body = (await (await request.get("/api/integrations")).json()) as {
    secrets_enabled: boolean;
    items: { capability: string; status: string }[];
  };
  expect(body.secrets_enabled, "AIOPS_SECRETS_KEY must be set for the API").toBe(true);
  expect(body.items.map((i) => i.capability)).toEqual(
    expect.arrayContaining(["logs", "metrics", "alerts", "k8s", "code", "tickets", "knowledge"]),
  );
  const tickets = body.items.find((i) => i.capability === "tickets");
  expect(tickets?.status).toMatch(/ok|connected/i);
});

test("UI: test connection against the live server, save a secret, it stays masked", async ({
  page,
  request,
}) => {
  await page.goto("/settings/integrations");
  await page.getByRole("button", { name: "Configure Tickets" }).click();
  const dialog = page.getByTestId("integration-dialog");

  await dialog.getByRole("button", { name: "Test connection" }).click();
  const results = dialog.getByTestId("test-results");
  await expect(results).toContainText("Test pass", { timeout: 20_000 });

  await dialog.getByLabel("Secret value").fill(SECRET);
  await dialog.getByRole("button", { name: "Set secret" }).click();
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("Tickets saved")).toBeVisible();
  await expect(page.getByTestId("secret-tickets-Authorization")).toHaveText(
    `Authorization: ••••${SECRET.slice(-4)}`,
  );
  await expect(page.locator("body")).not.toContainText(SECRET.slice(0, -4));

  // never in any API response: the item, the list, the audit
  for (const path of [
    "/api/integrations/tickets",
    "/api/integrations",
    "/api/integrations/audit?capability=tickets",
  ]) {
    const text = await (await request.get(path)).text();
    expect(text, path).not.toContain(SECRET.slice(0, -4));
  }
  // the saved header is actually used: the live server still passes with it
  const test = await request.post("/api/integrations/tickets/test", { data: {} });
  expect(((await test.json()) as { status: string }).status).not.toBe("fail");
});

test("a saved override applies to the running API without a restart, and reset reverts it", async ({
  request,
}) => {
  const dead = "http://127.0.0.1:9/mcp";
  const saved = await request.put("/api/integrations/tickets", {
    data: { fields: { "mcp.url": dead } },
  });
  expect(saved.ok(), await saved.text()).toBeTruthy();

  // no draft in the body: the test uses the SAVED config of the running process
  const broken = (await (
    await request.post("/api/integrations/tickets/test", { data: {} })
  ).json()) as { status: string; checks: { check: string; detail: string }[] };
  expect(broken.status).toBe("fail");
  expect(broken.checks.map((c) => c.detail).join(" ")).toContain("127.0.0.1:9");

  await reset(request);
  const fixed = (await (
    await request.post("/api/integrations/tickets/test", { data: {} })
  ).json()) as { status: string };
  expect(fixed.status).not.toBe("fail");
});

test("invalid values are refused with a readable 422, nothing is stored", async ({ request }) => {
  const res = await request.put("/api/integrations/tickets", {
    data: { fields: { "mcp.timeout_s": -5 } },
  });
  expect(res.status()).toBe(422);
  const item = (await (await request.get("/api/integrations/tickets")).json()) as {
    overridden: string[];
  };
  expect(item.overridden).toEqual([]);
});

test("every change is audited by field and actor, never the value", async ({ request }) => {
  await request.put("/api/integrations/tickets", {
    data: { fields: { "mcp.timeout_s": 12 }, secrets: { Authorization: SECRET } },
  });
  const audit = (await (
    await request.get("/api/integrations/audit?capability=tickets&limit=10")
  ).json()) as { actor: string; field?: string; changes?: unknown }[];
  const text = JSON.stringify(audit);
  expect(text).toContain("mcp.timeout_s");
  expect(text).toContain("Authorization");
  expect(text).not.toContain(SECRET.slice(0, -4));
  expect(audit[0]?.actor).toBeTruthy();
});
