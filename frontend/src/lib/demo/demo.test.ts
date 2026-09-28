import { describe, expect, it } from "vitest";

import s1 from "@/demo/scenario-S1.json";
import { Investigation, LiveEvent } from "@/lib/api/schemas";

import { detectService, routeQuestion } from "./route";
import {
  scheduleSession,
  sessionInvestigation,
  type DemoSession,
  type Recording,
} from "./schedule";
import { dayShift, shiftTimes } from "./time";

const rec: Recording = {
  investigation: Investigation.parse(s1.investigation),
  events: s1.events.map((e) => LiveEvent.parse(e)),
};

describe("routeQuestion", () => {
  it.each([
    ["Payment API is returning HTTP 500 in production", "S1"],
    ["Is anything wrong with payment-service in production?", "S0"],
    ["Orders are failing intermittently in production", "S2"],
    ["Why are orders timing out in production?", "S3"],
    ["Login and checkout requests are failing in production", "S4"],
    ["Payments are slow in production", "S5"],
  ])("%s → %s", (q, expected) => {
    expect(routeQuestion(q).scenario).toBe(expected);
  });

  it("asks for clarification instead of inventing a service (UC-13)", () => {
    const r = routeQuestion("Something is broken");
    expect(r.scenario).toBe("clarify");
    expect(detectService("Something is broken")).toBeNull();
  });

  it("prefers the explicit service chip", () => {
    expect(routeQuestion("errors everywhere", "user-service").scenario).toBe("S4");
  });
});

describe("time shifting", () => {
  it("moves ISO timestamps and dates by whole days, never into the future", () => {
    const anchor = "2026-09-28T10:45:00Z";
    expect(dayShift(anchor, Date.parse("2026-09-28T09:00:00Z"))).toBe(0);
    expect(dayShift(anchor, Date.parse("2026-10-01T11:00:00Z"))).toBe(3 * 86_400_000);
    const out = shiftTimes(
      { at: "2026-09-28T10:00:00Z", day: "2026-09-28", text: "10:00", n: 1 },
      86_400_000,
    );
    expect(out).toEqual({ at: "2026-09-29T10:00:00.000Z", day: "2026-09-29", text: "10:00", n: 1 });
  });
});

describe("scheduleSession", () => {
  const base: DemoSession = {
    id: "inv-new",
    incidentId: "INC-9",
    question: "Payment API is returning HTTP 500 in production",
    route: { scenario: "S1", service: "payment-service" },
    createdAt: 1_000_000,
  };

  it("replays the recording on the session clock, sped up", () => {
    const list = scheduleSession(base, rec, 4);
    expect(list).toHaveLength(rec.events.length);
    expect(list[0]!.event.investigation_id).toBe("inv-new");
    expect(list[0]!.due).toBe(base.createdAt);
    const recorded =
      Date.parse(rec.events.at(-1)!.timestamp) - Date.parse(rec.events[0]!.timestamp);
    expect(list.at(-1)!.due - base.createdAt).toBeCloseTo(recorded / 4, 0);
    expect(list.map((x) => x.event.seq)).toEqual(list.map((_, i) => i + 1));
  });

  it("pauses at clarification_needed until answered, then continues the resolved scenario", () => {
    const s: DemoSession = {
      ...base,
      question: "Something is broken",
      route: { scenario: "clarify", candidates: ["payment-service"] },
    };
    const waiting = scheduleSession(s, null, 4);
    expect(waiting.map((x) => x.event.type)).toEqual([
      "investigation_started",
      "clarification_needed",
    ]);
    const answered = scheduleSession(
      { ...s, clarifiedAt: 2_000_000, resolved: { scenario: "S1", service: "payment-service" } },
      rec,
      4,
    );
    expect(answered[2]!.event.type).toBe("plan_created");
    expect(answered[2]!.event.seq).toBe(3);
    expect(answered.at(-1)!.event.type).toBe("investigation_finished");
  });

  it("cuts the stream at cancel time with a cancelled finish", () => {
    const list = scheduleSession({ ...base, cancelledAt: base.createdAt + 3000 }, rec, 4);
    const last = list.at(-1)!.event;
    expect(last.type).toBe("investigation_finished");
    expect(last.type === "investigation_finished" && last.data.status).toBe("cancelled");
    expect(list.every((x) => x.due <= base.createdAt + 3000)).toBe(true);
  });

  it("exposes the full investigation only once the replay finished", () => {
    const running = sessionInvestigation(base, rec, 4, base.createdAt + 1000);
    expect(running.status).toBe("running");
    expect(running.report).toBeNull();
    const done = sessionInvestigation(base, rec, 4, base.createdAt + 10 * 60_000);
    expect(done.status).toBe("completed");
    expect(done.id).toBe("inv-new");
    expect(done.report?.root_cause_hypothesis_id).toBe("hy-s1-1");
  });
});
