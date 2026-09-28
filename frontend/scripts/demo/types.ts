import type {
  AgentStatus,
  Evidence,
  Finding,
  Hypothesis,
  Recommendation,
  Report,
  TimelineEvent,
} from "../../src/lib/api/schemas";

export type AgentName = "logs" | "metrics" | "alerts" | "k8s" | "code" | "tickets" | "knowledge";

export const CAPABILITY: Record<AgentName, string> = {
  logs: "logs",
  metrics: "metrics",
  alerts: "alerts",
  k8s: "k8s",
  code: "code",
  tickets: "tickets",
  knowledge: "knowledge",
};

export interface AgentSpec {
  agent: AgentName;
  objective: string;
  round?: number;
  /** ms after the investigation was created */
  start: number;
  duration: number;
  /** [tool name, duration ms, arguments] */
  tools: [string, number, Record<string, unknown>?][];
  status: AgentStatus;
  summary: string;
  signals: string[];
  evidence: Evidence[];
  findings: Omit<Finding, "id">[];
}

export interface ScenarioSpec {
  id: string;
  title: string;
  description: string;
  question: string;
  service: string;
  environment: string;
  created: number;
  windowStart: number;
  windowEnd: number;
  symptoms: string[];
  agents: AgentSpec[];
  hypotheses: Hypothesis[];
  recommendations: Recommendation[];
  timeline: TimelineEvent[];
  report: Omit<Report, "markdown">;
  /** The root-cause label used on the dashboard (by_service.top_root_cause). */
  rootCauseLabel: string | null;
  /** Phrasings used for the synthetic history. */
  variants: string[];
}
