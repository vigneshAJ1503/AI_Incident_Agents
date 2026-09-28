/**
 * SSE → UI state machine for a live investigation (UC-11). Pure and unit-tested: the hook in
 * use-live.ts feeds it events; components render the state.
 *
 *   idle → connecting → (clarifying ⇄) planning → investigating → rca → done | failed | cancelled
 */
import type {
  Evidence,
  Hypothesis,
  IncidentContext,
  InvestigationStatus,
  LiveEvent,
  Report,
} from "@/lib/api/schemas";
import { humanizeSignal } from "@/lib/format";

export type LanePhase = "queued" | "running" | "done" | "failed" | "skipped" | "cancelled";

export interface ToolRun {
  tool: string;
  status: string;
  duration_ms: number;
}

export interface Lane {
  stepId: string;
  agent: string;
  objective: string;
  round: number;
  phase: LanePhase;
  currentTool: string | null;
  tools: ToolRun[];
  signals: string[];
  evidenceCount: number;
  summary: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  durationMs: number | null;
}

export type Phase =
  | "idle"
  | "connecting"
  | "clarifying"
  | "planning"
  | "investigating"
  | "rca"
  | "done"
  | "failed"
  | "cancelled";

export interface LogEntry {
  seq: number;
  timestamp: string;
  type: LiveEvent["type"];
  agent: string | null;
  text: string;
  tone: "info" | "ok" | "warn" | "danger" | "muted";
}

export interface LiveState {
  phase: Phase;
  question: string | null;
  clarification: { question: string; candidates: string[] } | null;
  context: IncidentContext | null;
  lanes: Record<string, Lane>;
  order: string[];
  rounds: { round: number; agents: string[] }[];
  currentRound: number;
  evidence: (Evidence & { agent: string | null; stepId: string })[];
  hypotheses: Hypothesis[];
  report: Report | null;
  approvals: { approval_id: string; action: string }[];
  log: LogEntry[];
  lastSeq: number;
  error: { message: string; recoverable: boolean } | null;
  finalStatus: InvestigationStatus | null;
  durationMs: number | null;
}

export const initialLiveState: LiveState = {
  phase: "idle",
  question: null,
  clarification: null,
  context: null,
  lanes: {},
  order: [],
  rounds: [],
  currentRound: 0,
  evidence: [],
  hypotheses: [],
  report: null,
  approvals: [],
  log: [],
  lastSeq: 0,
  error: null,
  finalStatus: null,
  durationMs: null,
};

export type LiveAction =
  { type: "connect" } | { type: "event"; event: LiveEvent } | { type: "reset" };

const MAX_LOG = 400;

function describe(e: LiveEvent): Pick<LogEntry, "text" | "tone"> | null {
  switch (e.type) {
    case "investigation_started":
      return { text: `Investigation started: “${e.data.question}”`, tone: "info" };
    case "clarification_needed":
      return { text: `Needs clarification: ${e.data.question}`, tone: "warn" };
    case "plan_created":
      return {
        text: `Plan: ${e.data.steps.length} steps for ${e.data.context?.service ?? "the service"} (${e.data.context?.environment ?? "env"})`,
        tone: "info",
      };
    case "round_started":
      return { text: `Round ${e.data.round}: ${e.data.agents.join(", ")}`, tone: "info" };
    case "agent_started":
      return { text: `${e.agent ?? "agent"} started: ${e.data.objective}`, tone: "muted" };
    case "tool_called":
      return {
        text: `${e.agent ?? "agent"} → ${e.data.tool} (${e.data.status}, ${Math.round(e.data.duration_ms)} ms)`,
        tone: e.data.status === "ok" ? "muted" : "danger",
      };
    case "evidence_added":
      return {
        text: `${e.agent ?? "agent"} evidence ${e.data.evidence.id}: ${e.data.evidence.summary}`,
        tone: "info",
      };
    case "agent_finished": {
      const signals = e.data.signals.length
        ? ` · ${e.data.signals.map(humanizeSignal).join(", ")}`
        : "";
      return {
        text: `${e.agent ?? "agent"} ${e.data.status} (${e.data.evidence_count} evidence)${signals}`,
        tone: e.data.status === "failed" ? "danger" : e.data.status === "success" ? "ok" : "muted",
      };
    }
    case "rca_started":
      return { text: "RCA agent correlating findings…", tone: "info" };
    case "hypothesis_ranked": {
      const top = e.data.hypotheses[0];
      return {
        text: top
          ? `Top hypothesis (${Math.round(top.confidence * 100)}%): ${top.statement}`
          : "No hypothesis: nothing abnormal found",
        tone: "info",
      };
    }
    case "report_ready":
      return { text: "Report ready", tone: "ok" };
    case "approval_requested":
      return { text: `Approval requested: ${e.data.action}`, tone: "warn" };
    case "investigation_finished":
      return {
        text: `Investigation ${e.data.status}`,
        tone:
          e.data.status === "completed" ? "ok" : e.data.status === "cancelled" ? "muted" : "danger",
      };
    case "error":
      return { text: `Error: ${e.data.message}`, tone: "danger" };
    case "heartbeat":
      return null;
  }
}

function updateLane(
  state: LiveState,
  stepId: string,
  fn: (l: Lane) => Lane,
  fallback?: Partial<Lane>,
): LiveState {
  const existing = state.lanes[stepId];
  const base: Lane = existing ?? {
    stepId,
    agent: fallback?.agent ?? "agent",
    objective: fallback?.objective ?? "",
    round: fallback?.round ?? 1,
    phase: "queued",
    currentTool: null,
    tools: [],
    signals: [],
    evidenceCount: 0,
    summary: null,
    startedAt: null,
    finishedAt: null,
    durationMs: null,
  };
  return {
    ...state,
    lanes: { ...state.lanes, [stepId]: fn(base) },
    order: existing ? state.order : [...state.order, stepId],
  };
}

const closeLanes = (lanes: Record<string, Lane>, phase: LanePhase): Record<string, Lane> =>
  Object.fromEntries(
    Object.entries(lanes).map(([k, l]) => [
      k,
      l.phase === "running" || l.phase === "queued" ? { ...l, phase, currentTool: null } : l,
    ]),
  );

export function liveReducer(state: LiveState, action: LiveAction): LiveState {
  if (action.type === "reset") return initialLiveState;
  if (action.type === "connect")
    return state.phase === "idle" ? { ...state, phase: "connecting" } : state;

  const e = action.event;
  if (e.seq <= state.lastSeq) return state; // duplicate or replayed
  let s: LiveState = { ...state, lastSeq: e.seq };
  const d = describe(e);
  if (d) {
    const entry: LogEntry = {
      seq: e.seq,
      timestamp: e.timestamp,
      type: e.type,
      agent: e.agent ?? null,
      ...d,
    };
    s.log = [...s.log, entry].slice(-MAX_LOG);
  }

  switch (e.type) {
    case "investigation_started":
      return {
        ...s,
        phase: s.phase === "idle" || s.phase === "connecting" ? "planning" : s.phase,
        question: e.data.question,
      };
    case "clarification_needed":
      return {
        ...s,
        phase: "clarifying",
        clarification: { question: e.data.question, candidates: e.data.candidates },
      };
    case "plan_created": {
      s = { ...s, phase: "planning", clarification: null, context: e.data.context ?? s.context };
      for (const st of e.data.steps) {
        s = updateLane(
          s,
          st.id,
          (l) => ({ ...l, agent: st.agent, objective: st.objective, round: st.round }),
          st,
        );
      }
      return s;
    }
    case "round_started": {
      const rounds = [...s.rounds.filter((r) => r.round !== e.data.round), e.data].sort(
        (a, b) => a.round - b.round,
      );
      return {
        ...s,
        phase: "investigating",
        clarification: null,
        rounds,
        currentRound: e.data.round,
      };
    }
    case "agent_started":
      return updateLane(
        { ...s, phase: "investigating" },
        e.data.step_id,
        (l) => ({
          ...l,
          phase: "running",
          objective: e.data.objective || l.objective,
          round: e.data.round,
          startedAt: e.timestamp,
        }),
        { agent: e.agent ?? "agent", objective: e.data.objective, round: e.data.round },
      );
    case "tool_called":
      return updateLane(
        s,
        e.data.step_id,
        (l) => ({
          ...l,
          phase: l.phase === "queued" ? "running" : l.phase,
          currentTool: e.data.tool,
          tools: [
            ...l.tools,
            { tool: e.data.tool, status: e.data.status, duration_ms: e.data.duration_ms },
          ],
        }),
        { agent: e.agent ?? "agent" },
      );
    case "evidence_added": {
      const ev = { ...e.data.evidence, agent: e.agent ?? null, stepId: e.data.step_id };
      s = {
        ...s,
        evidence: s.evidence.some((x) => x.id === ev.id) ? s.evidence : [...s.evidence, ev],
      };
      return updateLane(s, e.data.step_id, (l) => ({ ...l, evidenceCount: l.evidenceCount + 1 }), {
        agent: e.agent ?? "agent",
      });
    }
    case "agent_finished":
      return updateLane(
        s,
        e.data.step_id,
        (l) => ({
          ...l,
          phase: e.data.status === "failed" ? "failed" : "done",
          currentTool: null,
          signals: e.data.signals,
          summary: e.data.summary,
          evidenceCount: Math.max(l.evidenceCount, e.data.evidence_count),
          finishedAt: e.timestamp,
          durationMs: e.data.duration_ms,
        }),
        { agent: e.agent ?? "agent" },
      );
    case "rca_started":
      return { ...s, phase: "rca" };
    case "hypothesis_ranked":
      return {
        ...s,
        hypotheses: [...e.data.hypotheses].sort((a, b) => b.confidence - a.confidence),
      };
    case "report_ready":
      return { ...s, report: e.data.report };
    case "approval_requested":
      return { ...s, approvals: [...s.approvals, e.data] };
    case "investigation_finished": {
      const st = e.data.status;
      const phase: Phase = st === "cancelled" ? "cancelled" : st === "failed" ? "failed" : "done";
      return {
        ...s,
        phase,
        finalStatus: st,
        durationMs: e.data.duration_ms,
        lanes:
          phase === "done"
            ? s.lanes
            : closeLanes(s.lanes, phase === "cancelled" ? "cancelled" : "skipped"),
      };
    }
    case "error":
      return { ...s, error: e.data, phase: e.data.recoverable ? s.phase : "failed" };
    case "heartbeat":
      return s;
  }
}

/** Derived numbers for the progress header. */
export function progress(state: LiveState): { done: number; total: number; ratio: number } {
  const lanes = Object.values(state.lanes);
  const total = lanes.length;
  const done = lanes.filter((l) => l.phase !== "queued" && l.phase !== "running").length;
  const rcaBonus =
    state.phase === "done" ? 1 : state.report ? 0.9 : state.phase === "rca" ? 0.5 : 0;
  const ratio = total === 0 ? 0 : Math.min(1, (done + rcaBonus) / (total + 1));
  return { done, total, ratio };
}

export function isTerminal(phase: Phase): boolean {
  return phase === "done" || phase === "failed" || phase === "cancelled";
}
