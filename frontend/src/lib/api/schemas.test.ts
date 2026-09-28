import { describe, expect, it } from "vitest";

import dashboard from "@/demo/dashboard.json";
import health from "@/demo/health.json";
import investigations from "@/demo/investigations.json";
import s1 from "@/demo/scenario-S1.json";

import { ContractError } from "./client";
import { parseOrThrow } from "./parse";
import { DashboardSummary, Health, Investigation, InvestigationPage, LiveEvent } from "./schemas";

describe("contract schemas", () => {
  it("accept the demo dataset", () => {
    expect(() => parseOrThrow(DashboardSummary, dashboard, "dashboard")).not.toThrow();
    expect(() => parseOrThrow(Health, health, "health")).not.toThrow();
    const page = parseOrThrow(InvestigationPage, { items: investigations.items }, "list");
    expect(page.items.length).toBeGreaterThanOrEqual(40);
  });

  it("parse the S1 investigation with its report and events", () => {
    const inv = parseOrThrow(Investigation, s1.investigation, "S1");
    expect(inv.report?.root_cause_hypothesis_id).toBe("hy-s1-1");
    expect(inv.report?.confidence).toBeGreaterThanOrEqual(0.8);
    const types = s1.events.map((e) => parseOrThrow(LiveEvent, e, "event").type);
    expect(types[0]).toBe("investigation_started");
    expect(types.at(-1)).toBe("investigation_finished");
    expect(types).toContain("report_ready");
  });

  it("fill contract defaults for optional fields", () => {
    const inv = Investigation.parse({
      id: "inv-1",
      incident: { id: "INC-1", title: "x" },
      status: "pending",
      created_at: "2026-09-28T10:00:00Z",
    });
    expect(inv.steps).toEqual([]);
    expect(inv.mode).toBe("live");
    expect(inv.usage.calls).toBe(0);
  });

  it("turn a contract mismatch into a readable ContractError", () => {
    const bad = { ...s1.investigation, status: "exploded" };
    expect(() => parseOrThrow(Investigation, bad, "GET /investigations/x")).toThrow(ContractError);
    try {
      parseOrThrow(Investigation, bad, "GET /investigations/x");
    } catch (e) {
      expect((e as Error).message).toContain("GET /investigations/x");
      expect((e as Error).message).toContain("status");
    }
  });

  it("reject unknown event types", () => {
    const r = LiveEvent.safeParse({
      type: "made_up",
      investigation_id: "x",
      timestamp: "2026-09-28T10:00:00Z",
      seq: 1,
      data: {},
    });
    expect(r.success).toBe(false);
  });
});
