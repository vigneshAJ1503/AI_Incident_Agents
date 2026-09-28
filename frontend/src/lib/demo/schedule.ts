/** Demo only: turn a recorded SSE stream into a real-time schedule for a simulated investigation. */
import type { Investigation, LiveEvent } from "../api/schemas";
import { shiftTimes } from "./time";
import type { Route } from "./route";

export interface Recording {
  investigation: Investigation;
  events: LiveEvent[];
}

export interface DemoSession {
  id: string;
  incidentId: string;
  question: string;
  route: Route;
  createdAt: number;
  clarifiedAt?: number;
  /** Set once a clarification resolved the scenario. */
  resolved?: { scenario: Exclude<Route["scenario"], "clarify">; service: string };
  cancelledAt?: number;
}

export interface Scheduled {
  event: LiveEvent;
  due: number;
}

const CLARIFY_DELAY_MS = 700;

export function clarificationQuestion(): string {
  return "Which service is affected, and since when? Firing alerts point to these candidates.";
}

/** Recording start (ms) of a scenario: its investigation's created_at. */
const t0 = (rec: Recording) => Date.parse(rec.investigation.created_at);

/**
 * The full, timed event list of a session. Recorded offsets are divided by `speed`; the session's
 * events reuse the recording's payloads, moved to the session's clock and id.
 */
export function scheduleSession(s: DemoSession, rec: Recording | null, speed: number): Scheduled[] {
  const out: Scheduled[] = [];
  const mk = (
    type: LiveEvent["type"],
    due: number,
    data: unknown,
    agent: string | null = null,
  ): Scheduled => ({
    due,
    event: {
      type,
      investigation_id: s.id,
      timestamp: new Date(due).toISOString(),
      seq: out.length + 1,
      agent,
      data,
    } as LiveEvent,
  });

  let start = s.createdAt;
  let skipStarted = false;
  if (s.route.scenario === "clarify") {
    out.push(mk("investigation_started", s.createdAt, { question: s.question }));
    out.push(
      mk("clarification_needed", s.createdAt + CLARIFY_DELAY_MS, {
        question: clarificationQuestion(),
        candidates: s.route.candidates,
      }),
    );
    if (!s.clarifiedAt || !rec) return applyCancel(s, out);
    start = s.clarifiedAt;
    skipStarted = true;
  }
  if (!rec) return applyCancel(s, out);

  const base = t0(rec);
  const shifted = shiftTimes(rec.events, start - base);
  for (const e of shifted) {
    if (skipStarted && e.type === "investigation_started") continue;
    const offset = (Date.parse(e.timestamp) - start) / speed;
    const due = start + offset;
    const { type, agent, data } = e;
    out.push(
      mk(
        type,
        due,
        type === "investigation_started" ? { question: s.question } : data,
        agent ?? null,
      ),
    );
  }
  return applyCancel(s, out);
}

function applyCancel(s: DemoSession, list: Scheduled[]): Scheduled[] {
  if (!s.cancelledAt) return list;
  const kept = list.filter(
    (x) => x.due <= s.cancelledAt! && x.event.type !== "investigation_finished",
  );
  const due = s.cancelledAt;
  kept.push({
    due,
    event: {
      type: "investigation_finished",
      investigation_id: s.id,
      timestamp: new Date(due).toISOString(),
      seq: kept.length + 1,
      agent: null,
      data: { status: "cancelled", duration_ms: due - s.createdAt },
    },
  });
  return kept;
}

/** Build the session's Investigation as the API would return it at `now`. */
export function sessionInvestigation(
  s: DemoSession,
  rec: Recording | null,
  speed: number,
  now: number,
): Investigation {
  const schedule = scheduleSession(s, rec, speed);
  const finished = schedule.find((x) => x.event.type === "investigation_finished");
  const incident = {
    id: s.incidentId,
    title: s.question,
    description: "",
    service: s.resolved?.service ?? (s.route.scenario === "clarify" ? null : s.route.service),
    environment: s.route.scenario === "clarify" && !s.resolved ? null : "production",
    source: "user" as const,
    created_at: new Date(s.createdAt).toISOString(),
  };
  const stub: Investigation = {
    id: s.id,
    incident,
    context: null,
    status: s.route.scenario === "clarify" && !s.clarifiedAt ? "needs_clarification" : "running",
    steps: [],
    results: [],
    hypotheses: [],
    recommendations: [],
    timeline: [],
    report: null,
    clarification_question:
      s.route.scenario === "clarify" && !s.clarifiedAt ? clarificationQuestion() : null,
    clarification_candidates:
      s.route.scenario === "clarify" && !s.clarifiedAt ? s.route.candidates : [],
    claims: [],
    versions: {},
    usage: { input_tokens: 0, output_tokens: 0, calls: 0 },
    duration_ms: null,
    created_at: incident.created_at,
    completed_at: null,
    mode: "demo",
  };
  if (s.cancelledAt && finished) {
    return {
      ...stub,
      status: "cancelled",
      completed_at: finished.event.timestamp,
      duration_ms: s.cancelledAt - s.createdAt,
    };
  }
  if (!rec || !finished || finished.due > now) return stub;

  const start = s.clarifiedAt ?? s.createdAt;
  const full = shiftTimes(rec.investigation, start - t0(rec));
  const duration = finished.due - s.createdAt;
  return {
    ...full,
    id: s.id,
    incident: {
      ...full.incident,
      id: s.incidentId,
      title: s.question,
      created_at: incident.created_at,
    },
    context: full.context ? { ...full.context, question: s.question } : null,
    created_at: incident.created_at,
    completed_at: new Date(finished.due).toISOString(),
    duration_ms: Math.round(duration),
  };
}
