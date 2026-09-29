import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { IntegrationCard, SlackPlaceholderCard } from "@/components/integrations/integration-card";
import { IntegrationDialog } from "@/components/integrations/integration-dialog";
import integrationsJson from "@/demo/integrations.json";
import { DemoClient } from "@/lib/api/demo-client";
import { IntegrationList, type Integration } from "@/lib/api/schemas";
import { applyDemoUpdate, demoTestResult, secretHint } from "@/lib/demo/integrations";
import {
  buildUpdate,
  fieldLabel,
  fromInput,
  initialForm,
  maskedSecret,
  toInput,
} from "@/lib/integrations";

const demo = IntegrationList.parse(integrationsJson);
const byCap = (cap: string): Integration => {
  const item = demo.items.find((i) => i.capability === cap);
  if (!item) throw new Error(cap);
  return structuredClone(item);
};
const field = (item: Integration, key: string) => {
  const f = item.fields.find((x) => x.key === key);
  if (!f) throw new Error(key);
  return f;
};
// built at runtime: never a real credential
const SECRET = `Bearer demo-${"x".repeat(8)}-${Date.now()}-tail`;

describe("the demo dataset", () => {
  it("parses and lists the seven capabilities with a masked sample secret", () => {
    expect(demo.items.map((i) => i.capability)).toEqual([
      "logs",
      "metrics",
      "alerts",
      "k8s",
      "code",
      "tickets",
      "knowledge",
    ]);
    const tickets = byCap("tickets");
    expect(tickets.secrets[0]).toMatchObject({ name: "Authorization", last4: "9f2c" });
  });
});

describe("form helpers", () => {
  const logs = byCap("logs");

  it("labels, shows and parses values by type", () => {
    expect(fieldLabel("mcp.url")).toBe("MCP server URL");
    expect(fieldLabel("settings.fields.level")).toBe("fields.level");
    const levels = field(logs, "settings.error_levels");
    expect(toInput(levels, levels.value)).toBe("ERROR, FATAL, CRITICAL");
    expect(fromInput(levels, "error, fatal ,")).toEqual(["error", "fatal"]);
    const max = field(logs, "limits.max_results");
    expect(fromInput(max, "250")).toBe(250);
    expect(fromInput(max, "")).toBeNull();
  });

  it("builds the minimal update body", () => {
    const form = initialForm(logs);
    expect(buildUpdate(logs, form)).toBeNull();
    form.fields["settings.fields.level"] = "log.level";
    form.fields["limits.max_results"] = 1000; // equal to the current value: not sent
    form.secrets = { Authorization: SECRET, "": "ignored" };
    form.enabled = false;
    expect(buildUpdate(logs, form)).toEqual({
      enabled: false,
      fields: { "settings.fields.level": "log.level" },
      secrets: { Authorization: SECRET },
    });
  });

  it("never shows a secret, at most its last 4 characters", () => {
    expect(
      maskedSecret({ name: "A", configured: true, last4: "9f2c", source: "ui", usable: true }),
    ).toBe("••••9f2c");
    expect(
      maskedSecret({ name: "A", configured: true, last4: null, source: "ui", usable: true }),
    ).toBe("configured");
    expect(secretHint(SECRET)).toBe("tail");
    expect(secretHint("short")).toBeNull();
  });
});

describe("demo rules (same as the API)", () => {
  it("applies fields, masks secrets and never keeps the value", () => {
    const saved = applyDemoUpdate(
      byCap("logs"),
      { fields: { "settings.fields.level": "log.level" }, secrets: { Authorization: SECRET } },
      "demo-operator",
      "2026-09-29T12:00:00Z",
      true,
    );
    expect(saved.changes).toEqual([
      { field: "settings.fields.level", change: "set" },
      { field: "secrets.Authorization", change: "set" },
    ]);
    expect(JSON.stringify(saved)).not.toContain(SECRET);
    expect(saved.integration.secrets).toContainEqual(
      expect.objectContaining({ name: "Authorization", last4: "tail", source: "ui" }),
    );
    expect(saved.integration.overridden).toEqual([
      "settings.fields.level",
      "secrets.Authorization",
    ]);
  });

  it("refuses bad values and secrets without a key", () => {
    const logs = byCap("logs");
    expect(() =>
      applyDemoUpdate(logs, { fields: { "mcp.url": "ftp://x" } }, "a", "t", true),
    ).toThrow(/http\(s\) URL/);
    expect(() =>
      applyDemoUpdate(logs, { fields: { "limits.max_results": "x" } }, "a", "t", true),
    ).toThrow(/number/);
    expect(() =>
      applyDemoUpdate(logs, { secrets: { Authorization: SECRET } }, "a", "t", false),
    ).toThrow(/AIOPS_SECRETS_KEY/);
  });

  it("answers test connection like aiops doctor", () => {
    const ok = demoTestResult(byCap("metrics"));
    expect(ok.status).toBe("ok");
    expect(ok.checks.map((c) => c.check)).toEqual(["config", "reachability", "contract", "smoke"]);
    const down = demoTestResult(byCap("metrics"), {
      fields: { "mcp.url": "http://127.0.0.1:9/mcp" },
    });
    expect(down.status).toBe("fail");
    expect(down.checks[1]).toMatchObject({ check: "reachability", status: "fail" });
    expect(demoTestResult(byCap("code"), { enabled: false }).status).toBe("skip");
  });

  it("persists edits in the demo client without the secret value", async () => {
    const store = new Map<string, string>();
    const storage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
    };
    const client = new DemoClient({ speed: 1, latency: 0, storage });
    await client.saveIntegration("alerts", { secrets: { Authorization: SECRET } });
    expect([...store.values()].join()).not.toContain(SECRET);
    const again = new DemoClient({ speed: 1, latency: 0, storage });
    const alerts = (await again.integrations()).items.find((i) => i.capability === "alerts");
    expect(alerts?.secrets[0]).toMatchObject({ name: "Authorization", last4: "tail" });
  });
});

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("integration card and dialog", () => {
  it("shows provider, status and a masked secret on the card", async () => {
    const onConfigure = vi.fn();
    render(<IntegrationCard item={byCap("tickets")} onConfigure={onConfigure} />);
    const card = screen.getByTestId("integration-tickets");
    expect(within(card).getByText("mock")).toBeInTheDocument();
    expect(within(card).getByText("Connected")).toBeInTheDocument();
    expect(screen.getByTestId("secret-tickets-Authorization")).toHaveTextContent(
      "Authorization: ••••9f2c",
    );
    await userEvent.click(screen.getByRole("button", { name: "Configure Tickets" }));
    expect(onConfigure).toHaveBeenCalledWith("tickets");
  });

  it("marks Slack as coming in PR-048", () => {
    render(<SlackPlaceholderCard />);
    expect(screen.getByTestId("integration-slack")).toHaveTextContent("Coming soon");
  });

  it("enables Save only once something changed, and types secrets into a password field", async () => {
    wrap(
      <IntegrationDialog item={byCap("logs")} secretsEnabled open onOpenChange={() => undefined} />,
    );
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    const level = screen.getByLabelText("fields.level");
    await userEvent.clear(level);
    await userEvent.type(level, "log.level");
    expect(save).toBeEnabled();
    expect(screen.getByLabelText("Secret value")).toHaveAttribute("type", "password");
    expect(screen.getByText("Read-only tool allowlist")).toBeInTheDocument();
  });

  it("explains how to enable secrets when the API has no key", () => {
    wrap(
      <IntegrationDialog
        item={byCap("logs")}
        secretsEnabled={false}
        open
        onOpenChange={() => undefined}
      />,
    );
    expect(screen.queryByLabelText("Secret value")).toBeNull();
    expect(screen.getByRole("note")).toHaveTextContent("AIOPS_SECRETS_KEY");
  });
});
