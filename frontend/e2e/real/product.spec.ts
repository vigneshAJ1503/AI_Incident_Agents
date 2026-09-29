import { expect, test, type APIRequestContext } from "@playwright/test";

/**
 * The whole product against the real stack (make demo), beyond demo.spec.ts: platform
 * questions in the chat (PR-041 /ask), an S1 investigation with its cost and tokens (PR-041),
 * evidence navigation, the Jira approval confirmed in place, Prometheus metrics, idempotent
 * creation (PR-042b) and the theme. Works with or without an LLM key: without one every run
 * is a zero-token replay; with AIOPS_REPLAY_LLM=real the agents reason with the hosted LLM.
 */

const API = process.env.E2E_API_URL ?? "http://localhost:8000";
const REPORT_TIMEOUT = 240_000; // a free-tier LLM can take minutes; a replay takes seconds

test.describe.configure({ timeout: 300_000 });

async function metric(request: APIRequestContext, name: string): Promise<number> {
  const text = await (await request.get(`${API}/metrics`)).text();
  return text
    .split("\n")
    .filter((line) => line.startsWith(`${name}{`) || line.startsWith(`${name} `))
    .reduce((sum, line) => sum + Number(line.trim().split(/\s+/).pop()), 0);
}

test("platform question is answered in the chat, not investigated", async ({ page }) => {
  await page.goto("/investigations/new");
  await page.getByLabel("Your question").fill("what are the agents that are running now?");
  await page.keyboard.press("Enter");

  const conversation = page.getByTestId("conversation");
  await expect(conversation).toContainText("what are the agents that are running now?");
  await expect(page.getByTestId("answer-card")).toBeVisible();
  await expect(page).toHaveURL(/\/investigations\/new$/);
});

test("S1: live view → report with cost → evidence drawer → Jira approved in place", async ({
  page,
  request,
}) => {
  const runsBefore = await metric(request, "aiops_agent_runs_total");

  await page.goto("/investigations/new");
  await page.getByLabel("Your question").fill("Payment API is returning HTTP 500 in production");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/investigations\/inv-/);
  await expect(page.getByTestId("live-view")).toBeVisible();

  // report: the right root cause, confidence, and no Python timedelta repr in the impact
  await expect(page.getByTestId("report")).toBeVisible({ timeout: REPORT_TIMEOUT });
  const hero = page.getByTestId("root-cause");
  await expect(hero).toContainText(/connection pool|DB_POOL_SIZE/i);
  await expect(page.getByTestId("confidence")).toHaveText(/\d+%/);
  await expect(hero).not.toContainText(/\d day, \d+:\d\d:\d\d|\b\d+:\d\d:\d\d\b/);

  // PR-041: cost and tokens per agent
  const cost = page.getByTestId("cost-card");
  await cost.scrollIntoViewIfNeeded();
  await expect(cost).toContainText("Cost & tokens");
  await expect(cost).toContainText(/\$\d/);
  await expect(cost.locator("tbody tr")).not.toHaveCount(0);

  // evidence drawer: open a citation, step with the arrow keys
  await hero.getByTestId("citation").first().click();
  const drawer = page.getByTestId("evidence-drawer");
  await expect(drawer).toContainText(/1 of \d+/);
  await page.keyboard.press("ArrowRight");
  await expect(drawer).toContainText(/2 of \d+/);
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();

  // Jira: the confirmation replaces the buttons in the same dialog (no stacked dialog)
  await page.getByTestId("create-jira").click();
  const dialog = page.getByRole("dialog", { name: "Create a Jira ticket" });
  await expect(dialog).toContainText("jira_create_issue");
  await dialog.getByRole("button", { name: "Approve" }).click();
  await expect(dialog.getByTestId("inline-confirm")).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(1);
  await dialog.getByTestId("confirm-decision").click();
  await expect(page.getByText(/Created OPS-\d+/)).toBeVisible();

  // PR-041: the run shows up in the Prometheus metrics
  expect(await metric(request, "aiops_agent_runs_total")).toBeGreaterThan(runsBefore);
});

test("dashboard shows cost by agent", async ({ page }) => {
  await page.goto("/");
  const card = page.getByTestId("agent-cost");
  await card.scrollIntoViewIfNeeded();
  await expect(card).toContainText("Cost by agent");
  await expect(card.locator("tbody tr").first()).toBeVisible();
});

test("/metrics is Prometheus text with bounded labels (no investigation ids)", async ({
  request,
}) => {
  const res = await request.get(`${API}/metrics`);
  expect(res.ok()).toBeTruthy();
  const text = await res.text();
  for (const name of [
    "aiops_investigations_total",
    "aiops_agent_runs_total",
    "aiops_agent_duration_seconds",
  ]) {
    expect(text, name).toContain(name);
  }
  expect(text).not.toMatch(/inv-[0-9a-f]{6,}/);
  // the Web UI proxy never forwards it
  const proxied = await request.get("/api/metrics");
  expect(proxied.ok()).toBeFalsy();
});

test("Idempotency-Key: a retried create returns the same investigation", async ({ request }) => {
  const key = `e2e-${Date.now()}`;
  // a recorded scenario's question: without a live cluster the API runs in replay mode
  const body = { question: "Is anything wrong with payment-service in production?" };
  const headers = { "Idempotency-Key": key };
  const first = await request.post("/api/investigations", { data: body, headers });
  expect(first.status()).toBe(202);
  const second = await request.post("/api/investigations", { data: body, headers });
  expect(second.status()).toBe(202);
  expect(second.headers()["idempotent-replayed"]).toBe("true");
  expect(((await second.json()) as { id: string }).id).toBe(
    ((await first.json()) as { id: string }).id,
  );
  // same key, different body: rejected
  const reused = await request.post("/api/investigations", {
    data: { question: "Orders are failing intermittently in production" },
    headers,
  });
  expect(reused.status()).toBe(422);
  expect(((await reused.json()) as { error: { code: string } }).error.code).toBe(
    "idempotency_key_reused",
  );
});

test("theme toggle switches light/dark and persists across a reload", async ({ page }) => {
  await page.goto("/");
  const html = page.locator("html");
  const wasDark = ((await html.getAttribute("class")) ?? "").includes("dark");
  await page.getByTestId("theme-toggle").click();
  await expect(html).toHaveClass(wasDark ? /^(?!.*\bdark\b)/ : /\bdark\b/);
  await page.reload();
  await expect(html).toHaveClass(wasDark ? /^(?!.*\bdark\b)/ : /\bdark\b/);
});
