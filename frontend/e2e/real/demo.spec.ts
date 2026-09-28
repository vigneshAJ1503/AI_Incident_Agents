import { expect, test, type APIRequestContext } from "@playwright/test";

/**
 * The real product, end to end (make demo): the Web UI container → its /api proxy → the API
 * container → Postgres (seeded by `aiops demo seed`) and mock-tickets-mcp. Zero tokens: without
 * an LLM key every new investigation is a replay of the recorded fixtures.
 */

async function newestStoredS1(request: APIRequestContext): Promise<string> {
  const res = await request.get("/api/investigations?mode=demo&service=payment-service&limit=50");
  expect(res.ok()).toBeTruthy();
  const page = (await res.json()) as {
    items: { id: string; report: { summary: string } | null }[];
  };
  const s1 = page.items.find((i) => /connection pool/i.test(i.report?.summary ?? ""));
  expect(s1, "a seeded S1 (DB pool) investigation").toBeTruthy();
  return s1!.id;
}

test("the browser only talks to the web origin (same-origin /api proxy)", async ({ request }) => {
  const health = await request.get("/api/health");
  expect(health.ok()).toBeTruthy();
  const body = (await health.json()) as { store: string; llm: { configured: boolean } };
  expect(body.store).toBe("ok");
});

test("dashboard shows the seeded 14-day history", async ({ page, request }) => {
  const summary = (await (await request.get("/api/dashboard/summary?days=14")).json()) as {
    totals: { investigations: number };
  };
  expect(summary.totals.investigations).toBeGreaterThanOrEqual(40);

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByText("Investigations per day")).toBeVisible();
  await expect(page.locator(".recharts-bar-rectangle").first()).toBeVisible();
  await expect(page.getByText("payment-service").first()).toBeVisible();
});

test("a stored investigation report opens", async ({ page, request }) => {
  const id = await newestStoredS1(request);
  await page.goto(`/investigations/${id}`);
  await expect(page.getByTestId("report")).toBeVisible();
  await expect(page.getByTestId("root-cause")).toContainText(/connection pool|DB_POOL_SIZE/i);
  await expect(page.getByTestId("confidence")).toHaveText(/\d+%/);
});

test("new S1 replay: live SSE lanes → report → Jira draft approved into mock-tickets", async ({
  page,
}) => {
  await page.goto("/investigations/new");
  await page.getByLabel("Your question").fill("Payment API is returning HTTP 500 in production");
  await page.keyboard.press("Enter");

  await expect(page).toHaveURL(/\/investigations\/inv-/);
  // the live view renders agent lanes from the real SSE stream (AIOPS_REPLAY_TOOL_DELAY_S paces it)
  await expect(page.getByTestId("live-view")).toBeVisible();
  await expect(page.getByTestId("lane-logs-r1")).toBeVisible();
  await expect(page.getByRole("log", { name: "Investigation event log" })).toContainText("Round 1");

  await expect(page.getByTestId("report")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("root-cause")).toContainText(/connection pool|DB_POOL_SIZE/i);

  await page.getByTestId("create-jira").click();
  const dialog = page.getByRole("dialog", { name: "Create a Jira ticket" });
  await expect(dialog).toContainText("jira_create_issue");
  await dialog.getByRole("button", { name: "Approve" }).click();
  await page.getByTestId("confirm-decision").click();
  // executed through the approval framework by mock-tickets-mcp: a real OPS-n key comes back
  await expect(page.getByText(/Created OPS-\d+/)).toBeVisible();
});
