/**
 * Runtime schemas for the API contract v1 (docs/api/contract.md, docs/schemas/*.schema.json).
 * Everything the UI receives (REST, SSE, demo JSON) is parsed through these, so a contract drift
 * surfaces as a readable error instead of a broken screen. Objects are non-strict on purpose:
 * unknown fields are ignored so the backend can add fields without breaking the UI.
 */
import { z } from "zod";

const isoDate = z.string().min(1);
const nullableStr = z.string().nullable().optional();

export const EvidenceKind = z.enum([
  "log",
  "metric",
  "alert",
  "k8s_event",
  "commit",
  "ticket",
  "doc",
]);
export type EvidenceKind = z.infer<typeof EvidenceKind>;

export const ClaimKind = z.enum([
  "FACT",
  "OBSERVATION",
  "CORRELATION",
  "HYPOTHESIS",
  "RECOMMENDATION",
]);
export type ClaimKind = z.infer<typeof ClaimKind>;

export const Evidence = z.object({
  id: z.string(),
  kind: EvidenceKind,
  source: z.string(),
  summary: z.string(),
  query: nullableStr,
  link: nullableStr,
  timestamp: nullableStr,
  data: z.record(z.string(), z.unknown()).default({}),
});
export type Evidence = z.infer<typeof Evidence>;

export const Finding = z.object({
  id: z.string(),
  type: z.string(),
  kind: ClaimKind,
  description: z.string(),
  evidence_ids: z.array(z.string()).default([]),
  confidence: z.number().nullable().optional(),
});
export type Finding = z.infer<typeof Finding>;

export const TokenUsage = z.object({
  input_tokens: z.number().int().default(0),
  output_tokens: z.number().int().default(0),
  calls: z.number().int().default(0),
});
export type TokenUsage = z.infer<typeof TokenUsage>;

export const ToolCall = z.object({
  id: z.string(),
  agent: z.string(),
  capability: z.string(),
  tool: z.string(),
  arguments: z.record(z.string(), z.unknown()).default({}),
  status: z.enum(["ok", "error", "blocked", "timeout"]).default("ok"),
  duration_ms: z.number().default(0),
  result_chars: z.number().int().default(0),
  error: nullableStr,
  started_at: isoDate.optional(),
});
export type ToolCall = z.infer<typeof ToolCall>;

export const AgentStatus = z.enum(["success", "no_signal", "partial", "failed"]);
export type AgentStatus = z.infer<typeof AgentStatus>;

export const AgentResult = z.object({
  task_id: z.string(),
  agent: z.string(),
  agent_version: z.string().optional(),
  status: AgentStatus,
  summary: z.string(),
  findings: z.array(Finding).default([]),
  evidence: z.array(Evidence).default([]),
  signals: z.array(z.string()).default([]),
  suggested_followups: z.array(z.string()).default([]),
  tool_calls: z.array(ToolCall).default([]),
  confidence: z.number().nullable().optional(),
  model: nullableStr,
  prompt_version: nullableStr,
  usage: TokenUsage.default({ input_tokens: 0, output_tokens: 0, calls: 0 }),
  duration_ms: z.number().default(0),
  error: nullableStr,
});
export type AgentResult = z.infer<typeof AgentResult>;

export const StepStatus = z.enum(["queued", "running", "done", "failed", "skipped"]);
export type StepStatus = z.infer<typeof StepStatus>;

export const InvestigationStep = z.object({
  id: z.string(),
  agent: z.string(),
  objective: z.string(),
  round: z.number().int().default(1),
  depends_on: z.array(z.string()).default([]),
  status: StepStatus.default("queued"),
  started_at: nullableStr,
  finished_at: nullableStr,
});
export type InvestigationStep = z.infer<typeof InvestigationStep>;

export const Hypothesis = z.object({
  id: z.string(),
  statement: z.string(),
  confidence: z.number(),
  supporting_evidence_ids: z.array(z.string()).default([]),
  contradicting_evidence_ids: z.array(z.string()).default([]),
});
export type Hypothesis = z.infer<typeof Hypothesis>;

export const Risk = z.enum(["low", "medium", "high"]);
export const Recommendation = z.object({
  id: z.string(),
  action: z.string(),
  rationale: z.string().default(""),
  risk: Risk.default("low"),
  requires_approval: z.boolean().default(false),
  evidence_ids: z.array(z.string()).default([]),
});
export type Recommendation = z.infer<typeof Recommendation>;

export const TimelineEvent = z.object({
  timestamp: isoDate,
  description: z.string(),
  source: z.string(),
  evidence_id: nullableStr,
});
export type TimelineEvent = z.infer<typeof TimelineEvent>;

export const Incident = z.object({
  id: z.string(),
  title: z.string(),
  description: z.string().default(""),
  service: nullableStr,
  environment: nullableStr,
  source: z.enum(["user", "alert", "ticket"]).default("user"),
  created_at: isoDate.optional(),
});
export type Incident = z.infer<typeof Incident>;

export const TimeRange = z.object({ start: isoDate, end: isoDate });
export const IncidentContext = z.object({
  question: z.string(),
  service: nullableStr,
  environment: nullableStr,
  time_range: TimeRange,
  symptoms: z.array(z.string()).default([]),
});
export type IncidentContext = z.infer<typeof IncidentContext>;

export const InvestigationStatus = z.enum([
  "pending",
  "needs_clarification",
  "running",
  "completed",
  "partial",
  "failed",
  "cancelled",
]);
export type InvestigationStatus = z.infer<typeof InvestigationStatus>;

export const Severity = z.enum(["critical", "high", "medium", "low", "none"]);
export type Severity = z.infer<typeof Severity>;

export const Mode = z.enum(["live", "replay", "demo"]);
export type Mode = z.infer<typeof Mode>;

export const Report = z.object({
  summary: z.string(),
  root_cause_hypothesis_id: nullableStr,
  confidence: z.number().default(0),
  impact: z.string().default(""),
  affected_services: z.array(z.string()).default([]),
  severity: Severity.default("none"),
  next_steps: z.array(z.string()).default([]),
  open_questions: z.array(z.string()).default([]),
  markdown: z.string().default(""),
});
export type Report = z.infer<typeof Report>;

export const Investigation = z.object({
  id: z.string(),
  incident: Incident,
  context: IncidentContext.nullable().optional(),
  status: InvestigationStatus,
  steps: z.array(InvestigationStep).default([]),
  results: z.array(AgentResult).default([]),
  hypotheses: z.array(Hypothesis).default([]),
  recommendations: z.array(Recommendation).default([]),
  timeline: z.array(TimelineEvent).default([]),
  report: Report.nullable().optional(),
  clarification_question: nullableStr,
  /** PR-030: catalog services to pick from when the planner needs a clarification. */
  clarification_candidates: z.array(z.string()).default([]),
  /** PR-033: the report's typed claims (RCA agent); falls back to agent findings when empty. */
  claims: z.array(Finding).default([]),
  versions: z.record(z.string(), z.string()).default({}),
  usage: TokenUsage.default({ input_tokens: 0, output_tokens: 0, calls: 0 }),
  duration_ms: z.number().nullable().optional(),
  created_at: isoDate,
  completed_at: nullableStr,
  mode: Mode.default("live"),
});
export type Investigation = z.infer<typeof Investigation>;

export const InvestigationSummary = z.object({
  id: z.string(),
  incident: Incident,
  status: InvestigationStatus,
  report: z
    .object({
      summary: z.string().default(""),
      severity: Severity.default("none"),
      confidence: z.number().default(0),
    })
    .nullable()
    .optional(),
  affected_services: z.array(z.string()).default([]),
  created_at: isoDate,
  completed_at: nullableStr,
  duration_ms: z.number().nullable().optional(),
  mode: Mode.default("live"),
});
export type InvestigationSummary = z.infer<typeof InvestigationSummary>;

export const InvestigationPage = z.object({
  items: z.array(InvestigationSummary),
  next_cursor: z.string().nullable().optional(),
});
export type InvestigationPage = z.infer<typeof InvestigationPage>;

export const CapabilityState = z.enum(["ok", "down", "disabled"]);
export type CapabilityState = z.infer<typeof CapabilityState>;

export const Health = z.object({
  status: z.string(),
  version: z.string().default("unknown"),
  profile: z.string().default("unknown"),
  llm: z.object({ provider: z.string().nullable().optional(), configured: z.boolean() }),
  capabilities: z.record(z.string(), CapabilityState).default({}),
  /** Optional (contract addendum, PR-036): true when AIOPS_ENABLE_FAULTS=1. */
  faults_enabled: z.boolean().default(false),
});
export type Health = z.infer<typeof Health>;

export const Service = z.object({
  name: z.string(),
  description: z.string().default(""),
  owners: z.record(z.string(), z.string()).default({}),
  depends_on: z.array(z.string()).default([]),
  environments: z.array(z.string()).default([]),
  runbooks: z.array(z.string()).default([]),
});
export type Service = z.infer<typeof Service>;
export const ServiceList = z.array(Service);

export const Agent = z.object({
  name: z.string(),
  version: z.string().default(""),
  description: z.string().default(""),
  capabilities: z.array(z.string()).default([]),
  last_run_at: nullableStr,
  success_rate_7d: z.number().nullable().optional(),
  p50_ms: z.number().nullable().optional(),
});
export type Agent = z.infer<typeof Agent>;
export const AgentList = z.array(Agent);

export const DashboardSummary = z.object({
  window_days: z.number().int(),
  totals: z.object({
    investigations: z.number().int(),
    open: z.number().int(),
    root_cause_found: z.number().int(),
    no_incident: z.number().int(),
    failed: z.number().int(),
  }),
  mttr_minutes: z.object({ p50: z.number(), p90: z.number() }),
  avg_confidence: z.number(),
  by_day: z.array(
    z.object({
      date: z.string(),
      investigations: z.number().int(),
      critical: z.number().int().default(0),
      high: z.number().int().default(0),
      medium: z.number().int().default(0),
      low: z.number().int().default(0),
    }),
  ),
  by_service: z.array(
    z.object({
      service: z.string(),
      investigations: z.number().int(),
      top_root_cause: z.string().nullable().optional(),
    }),
  ),
  top_signals: z.array(z.object({ signal: z.string(), count: z.number().int() })),
  agents: z.array(
    z.object({
      name: z.string(),
      runs: z.number().int(),
      success_rate: z.number(),
      p50_ms: z.number(),
      tokens: z.number().int().default(0),
    }),
  ),
  recent: z.array(InvestigationSummary),
});
export type DashboardSummary = z.infer<typeof DashboardSummary>;

export const ApprovalStatus = z.enum(["pending", "approved", "denied", "executed", "failed"]);
export type ApprovalStatus = z.infer<typeof ApprovalStatus>;

export const Approval = z.object({
  id: z.string(),
  action: z.string(),
  capability: z.string(),
  tool: z.string(),
  arguments: z.record(z.string(), z.unknown()).default({}),
  reason: z.string().default(""),
  risk: Risk.default("low"),
  status: ApprovalStatus,
  requested_by: z.string().default("aiops"),
  investigation_id: nullableStr,
  created_at: isoDate,
  decided_by: nullableStr,
  decided_at: nullableStr,
  comment: nullableStr,
  result: z.record(z.string(), z.unknown()).nullable().optional(),
});
export type Approval = z.infer<typeof Approval>;
export const ApprovalList = z.array(Approval);

export const Scenario = z.object({
  id: z.string(),
  title: z.string(),
  service: z.string(),
  description: z.string().default(""),
  active: z.boolean().default(false),
});
export type Scenario = z.infer<typeof Scenario>;
export const ScenarioList = z.array(Scenario);

export const CreateInvestigationRequest = z.object({
  question: z.string().min(3),
  service: z.string().optional(),
  environment: z.string().optional(),
  since: z.string().optional(),
  start: z.string().optional(),
  end: z.string().optional(),
  mode: z.enum(["live", "replay"]).optional(),
});
export type CreateInvestigationRequest = z.infer<typeof CreateInvestigationRequest>;

export const CreateInvestigationResponse = z.object({
  id: z.string(),
  status: InvestigationStatus,
});
export type CreateInvestigationResponse = z.infer<typeof CreateInvestigationResponse>;

export const ApiErrorBody = z.object({
  error: z.object({ code: z.string(), message: z.string() }),
});

// ---- Live events (SSE) --------------------------------------------------------------------

const base = {
  investigation_id: z.string(),
  timestamp: isoDate,
  seq: z.number().int(),
  agent: z.string().nullable().optional(),
};

const ev = <T extends string, D extends z.ZodType>(type: T, data: D) =>
  z.object({ ...base, type: z.literal(type), data });

export const LiveEvent = z.discriminatedUnion("type", [
  ev("investigation_started", z.object({ question: z.string() })),
  ev(
    "clarification_needed",
    z.object({ question: z.string(), candidates: z.array(z.string()).default([]) }),
  ),
  ev(
    "plan_created",
    z.object({ context: IncidentContext.nullable().optional(), steps: z.array(InvestigationStep) }),
  ),
  ev("round_started", z.object({ round: z.number().int(), agents: z.array(z.string()) })),
  ev(
    "agent_started",
    z.object({ step_id: z.string(), objective: z.string().default(""), round: z.number().int() }),
  ),
  ev(
    "tool_called",
    z.object({
      step_id: z.string(),
      tool: z.string(),
      status: z.string().default("ok"),
      duration_ms: z.number().default(0),
    }),
  ),
  ev("evidence_added", z.object({ step_id: z.string(), evidence: Evidence })),
  ev(
    "agent_finished",
    z.object({
      step_id: z.string(),
      status: z.string(),
      summary: z.string().default(""),
      signals: z.array(z.string()).default([]),
      evidence_count: z.number().int().default(0),
      duration_ms: z.number().default(0),
      tokens: z.number().int().default(0),
    }),
  ),
  ev("rca_started", z.object({}).default({})),
  ev("hypothesis_ranked", z.object({ hypotheses: z.array(Hypothesis) })),
  ev("report_ready", z.object({ report: Report })),
  ev("approval_requested", z.object({ approval_id: z.string(), action: z.string() })),
  ev(
    "investigation_finished",
    z.object({ status: InvestigationStatus, duration_ms: z.number().default(0) }),
  ),
  ev("error", z.object({ message: z.string(), recoverable: z.boolean().default(false) })),
  ev("heartbeat", z.object({}).default({})),
]);
export type LiveEvent = z.infer<typeof LiveEvent>;
export type LiveEventType = LiveEvent["type"];
