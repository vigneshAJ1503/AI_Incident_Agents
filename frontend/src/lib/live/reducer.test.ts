import { describe, expect, it } from "vitest";

import s1 from "@/demo/scenario-S1.json";
import { LiveEvent, type LiveEvent as Ev } from "@/lib/api/schemas";

import { initialLiveState, isTerminal, liveReducer, progress, type LiveState } from "./reducer";

const events = s1.events.map((e) => LiveEvent.parse(e));
const run = (list: Ev[], from: LiveState = initialLiveState) =>
  list.reduce((s, event) => liveReducer(s, { type: "event", event }), from);

const mk = (seq: number, type: string, data: unknown, agent: string | null = null): Ev =>
  LiveEvent.parse({
    type,
    seq,
    investigation_id: "inv-1",
    timestamp: "2026-09-28T10:00:00Z",
    agent,
    data,
  });

describe("liveReducer on the recorded S1 stream", () => {
  const final = run(events);

  it("ends done with a report and the DB-pool hypothesis on top", () => {
    expect(final.phase).toBe("done");
    expect(final.finalStatus).toBe("completed");
    expect(final.report?.root_cause_hypothesis_id).toBe("hy-s1-1");
    expect(final.hypotheses[0]?.statement).toMatch(/connection pool/i);
    expect(isTerminal(final.phase)).toBe(true);
  });

  it("tracks every step as a lane, with rounds, tools, signals and evidence", () => {
    expect(final.order).toHaveLength(8);
    expect(final.rounds.map((r) => r.round)).toEqual([1, 2]);
    const logs = final.lanes["st-s1-logs-r1"]!;
    expect(logs.phase).toBe("done");
    expect(logs.tools).toHaveLength(4);
    expect(logs.signals).toContain("db_timeout_errors_up");
    expect(logs.evidenceCount).toBe(3);
    expect(final.evidence.length).toBeGreaterThan(20);
    expect(progress(final).ratio).toBe(1);
  });

  it("shows the running agent and its current tool mid-stream", () => {
    const i = events.findIndex((e) => e.type === "tool_called");
    const mid = run(events.slice(0, i + 1));
    const lane = Object.values(mid.lanes).find((l) => l.phase === "running" && l.currentTool);
    expect(mid.phase).toBe("investigating");
    expect(lane?.currentTool).toBe(events[i]?.type === "tool_called" ? events[i].data.tool : "");
    expect(progress(mid).ratio).toBeLessThan(0.5);
  });

  it("ignores duplicates and replays (seq <= lastSeq)", () => {
    const twice = run(events, final);
    expect(twice).toBe(final);
  });

  it("writes a readable, timestamped log without heartbeats", () => {
    expect(final.log[0]?.text).toContain("Investigation started");
    expect(final.log.at(-1)?.text).toBe("Investigation completed");
    const withHeartbeat = liveReducer(final, { type: "event", event: mk(999, "heartbeat", {}) });
    expect(withHeartbeat.log).toHaveLength(final.log.length);
  });
});

describe("liveReducer edge cases", () => {
  it("handles clarification then resumes on plan_created", () => {
    let s = run([
      mk(1, "investigation_started", { question: "Something is broken" }),
      mk(2, "clarification_needed", {
        question: "Which service?",
        candidates: ["payment-service"],
      }),
    ]);
    expect(s.phase).toBe("clarifying");
    expect(s.clarification?.candidates).toEqual(["payment-service"]);
    s = run(
      [
        mk(3, "plan_created", {
          context: null,
          steps: [{ id: "st-1", agent: "logs", objective: "x", round: 1 }],
        }),
      ],
      s,
    );
    expect(s.phase).toBe("planning");
    expect(s.clarification).toBeNull();
    expect(s.lanes["st-1"]?.phase).toBe("queued");
  });

  it("marks unfinished lanes cancelled when the run is cancelled", () => {
    const s = run([
      mk(1, "plan_created", {
        steps: [
          { id: "a", agent: "logs", objective: "x", round: 1 },
          { id: "b", agent: "k8s", objective: "y", round: 1 },
        ],
      }),
      mk(2, "agent_started", { step_id: "a", objective: "x", round: 1 }, "logs"),
      mk(3, "investigation_finished", { status: "cancelled", duration_ms: 1200 }),
    ]);
    expect(s.phase).toBe("cancelled");
    expect(s.lanes.a?.phase).toBe("cancelled");
    expect(s.lanes.b?.phase).toBe("cancelled");
  });

  it("creates a lane for an agent that wasn't in the plan and records failures", () => {
    const s = run([
      mk(1, "agent_started", { step_id: "x", objective: "late", round: 2 }, "tickets"),
      mk(
        2,
        "tool_called",
        { step_id: "x", tool: "jira_search", status: "timeout", duration_ms: 30000 },
        "tickets",
      ),
      mk(
        3,
        "agent_finished",
        {
          step_id: "x",
          status: "failed",
          summary: "timeout",
          signals: [],
          evidence_count: 0,
          duration_ms: 30000,
          tokens: 0,
        },
        "tickets",
      ),
    ]);
    expect(s.lanes.x?.agent).toBe("tickets");
    expect(s.lanes.x?.phase).toBe("failed");
    expect(s.log.some((l) => l.tone === "danger")).toBe(true);
  });

  it("a non-recoverable error fails the run; a recoverable one does not", () => {
    const base = run([mk(1, "investigation_started", { question: "q" })]);
    expect(run([mk(2, "error", { message: "boom", recoverable: true })], base).phase).toBe(
      "planning",
    );
    expect(run([mk(2, "error", { message: "boom", recoverable: false })], base).phase).toBe(
      "failed",
    );
  });
});
