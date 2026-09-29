/**
 * Generates the static demo dataset in src/demo/*.json (see src/demo/README.md).
 *
 *   npm run demo:generate
 *
 * Deterministic: a fixed seed and a fixed calendar (anchor 2026-09-28T10:45Z), so re-running it
 * produces byte-identical files. Every file is validated against the zod contract schemas before
 * it is written. `aiops demo export` (backend, later) can overwrite the same files from real
 * replay runs; the UI only depends on the file layout, not on this script.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { z } from "zod";

import {
  AgentList,
  ApprovalList,
  DashboardSummary,
  Health,
  Investigation,
  InvestigationSummary,
  LiveEvent,
  ScenarioList,
  ServiceList,
  type InvestigationStatus,
  type InvestigationSummary as Summary,
  type Severity,
} from "../src/lib/api/schemas";
import { buildScenario, TIME_SCALE } from "./demo/build";
import { DAY, HOUR, MIN, SEC, at, iso, mulberry32, round } from "./demo/rng";
import { SCENARIOS } from "./demo/scenarios";
import type { ScenarioSpec } from "./demo/types";

const OUT = join(dirname(fileURLToPath(import.meta.url)), "..", "src", "demo");
const ANCHOR = at("2026-09-28T10:45:00Z");
const rand = mulberry32(1503);

function write(name: string, schema: z.ZodType, data: unknown): void {
  const parsed = schema.safeParse(data);
  if (!parsed.success) {
    console.error(`${name} does not match the contract:`, z.prettifyError(parsed.error));
    process.exit(1);
  }
  mkdirSync(OUT, { recursive: true });
  writeFileSync(join(OUT, name), `${JSON.stringify(data)}\n`);
  console.log(`wrote src/demo/${name}`);
}

const byId = new Map(SCENARIOS.map((s) => [s.id, s]));
const spec = (id: string): ScenarioSpec => byId.get(id)!;

// ---- 1. Full scenario investigations + SSE recordings -----------------------------------------
const templates = new Map<string, ReturnType<typeof buildScenario>>();
let incidentNo = 1100;
for (const s of SCENARIOS) {
  const built = buildScenario(s, {
    investigation: `inv-demo-${s.id.toLowerCase()}`,
    incident: `INC-${incidentNo++}`,
  });
  templates.set(s.id, built);
  write(
    `scenario-${s.id}.json`,
    z.object({ investigation: Investigation, events: z.array(LiveEvent) }),
    built,
  );
}

// ---- 2. History: ~40 investigations over 14 days -----------------------------------------------
const summaryOf = (inv: Investigation): Summary => ({
  id: inv.id,
  incident: inv.incident,
  status: inv.status,
  report: inv.report
    ? {
        summary: inv.report.summary,
        severity: inv.report.severity,
        confidence: inv.report.confidence,
      }
    : null,
  affected_services: inv.report?.affected_services ?? [],
  created_at: inv.created_at,
  completed_at: inv.completed_at ?? null,
  duration_ms: inv.duration_ms ?? null,
  mode: inv.mode,
});

interface HistoryItem {
  summary: Summary;
  scenario: string;
  signals: string[];
}
const history: HistoryItem[] = [];
const signalsOf = (s: ScenarioSpec) => [...new Set(s.agents.flatMap((a) => a.signals))];

for (const s of SCENARIOS) {
  const t = templates.get(s.id)!;
  history.push({ summary: summaryOf(t.investigation), scenario: s.id, signals: signalsOf(s) });
}

const weights: [string, number][] = [
  ["S1", 0.3],
  ["S5", 0.15],
  ["S2", 0.15],
  ["S3", 0.15],
  ["S4", 0.12],
  ["S0", 0.13],
];
const pick = (): string => {
  let r = rand();
  for (const [id, w] of weights) {
    if ((r -= w) <= 0) return id;
  }
  return "S1";
};
const LOWER: Record<Severity, Severity> = {
  critical: "high",
  high: "medium",
  medium: "low",
  low: "low",
  none: "none",
};
/** Real incidents vary in blast radius: downgrade about a third of the synthetic ones. */
const vary = (s: Severity): Severity => {
  const r = rand();
  return r < 0.22 ? LOWER[s] : r < 0.3 ? LOWER[LOWER[s]] : s;
};
const failedSlots = new Set([7, 21]);
const partialSlot = 15;
for (let i = 0; i < 33; i++) {
  const sid = pick();
  const s = spec(sid);
  const t = templates.get(sid)!.investigation;
  const dayOffset = Math.floor((i / 33) * 13.5) + 1; // spread over the 14 days before the anchor
  const created = ANCHOR - dayOffset * DAY - Math.floor(rand() * 10 * HOUR) - 2 * HOUR;
  const duration = Math.round((35 + rand() * 105) * SEC);
  const status: InvestigationStatus = failedSlots.has(i)
    ? "failed"
    : i === partialSlot
      ? "partial"
      : "completed";
  const conf =
    t.report && t.report.confidence > 0
      ? round(Math.min(0.97, Math.max(0.55, t.report.confidence + (rand() - 0.5) * 0.14)), 2)
      : 0;
  const id = `inv-${(0x5a17c3 + i * 7919).toString(16)}${(1000 + i).toString(16)}`;
  const question = s.variants[Math.floor(rand() * s.variants.length)] ?? s.question;
  history.push({
    scenario: sid,
    signals: status === "failed" ? [] : signalsOf(s),
    summary: {
      id,
      incident: {
        ...t.incident,
        id: `INC-${incidentNo++}`,
        title: question,
        created_at: iso(created),
      },
      status,
      // partial: two agents failed, so the RCA has only a weak lead (the UI's detail view:
      // src/lib/demo/partial.ts); the same summary as the backend's build_report
      report:
        status === "failed" || !t.report
          ? null
          : status === "partial"
            ? {
                summary:
                  `Abnormal signals on ${s.service} in ${s.environment} but no root cause ` +
                  "identified: the evidence is too weak or inconsistent. Weak lead: " +
                  (t.hypotheses[0]?.statement ?? "none"),
                severity: vary(t.report.severity),
                confidence: 0,
              }
            : {
                summary: t.report.summary,
                severity: vary(t.report.severity),
                confidence: conf,
              },
      affected_services: status === "failed" ? [] : (t.report?.affected_services ?? []),
      created_at: iso(created),
      completed_at: iso(created + duration),
      duration_ms: duration,
      mode: "demo",
    },
  });
}

// the one open investigation: waiting for a clarification (UC-13)
const openCreated = ANCHOR - 4 * MIN;
history.push({
  scenario: "clarify",
  signals: [],
  summary: {
    id: "inv-demo-clarify",
    incident: {
      id: `INC-${incidentNo++}`,
      title: "Something is broken",
      description: "",
      service: null,
      environment: null,
      source: "user",
      created_at: iso(openCreated),
    },
    status: "needs_clarification",
    report: null,
    affected_services: [],
    created_at: iso(openCreated),
    completed_at: null,
    duration_ms: null,
    mode: "demo",
  },
});

history.sort((a, b) => b.summary.created_at.localeCompare(a.summary.created_at));
write(
  "investigations.json",
  z.object({ items: z.array(InvestigationSummary), scenario_of: z.record(z.string(), z.string()) }),
  {
    items: history.map((h) => h.summary),
    scenario_of: Object.fromEntries(history.map((h) => [h.summary.id, h.scenario])),
  },
);

// ---- 3. Dashboard ---------------------------------------------------------------------------
const days = 14;
const firstDay = new Date(ANCHOR - (days - 1) * DAY).toISOString().slice(0, 10);
const byDay = Array.from({ length: days }, (_, i) => {
  const date = new Date(at(`${firstDay}T00:00:00Z`) + i * DAY).toISOString().slice(0, 10);
  const items = history.filter((h) => h.summary.created_at.startsWith(date));
  const sev = (x: Severity) => items.filter((h) => h.summary.report?.severity === x).length;
  return {
    date,
    investigations: items.length,
    critical: sev("critical"),
    high: sev("high"),
    medium: sev("medium"),
    low: sev("low"),
  };
});
const done = history.filter((h) => h.summary.duration_ms);
const mttr = done.map((h) => h.summary.duration_ms! / MIN).sort((a, b) => a - b);
const pct = (arr: number[], p: number) =>
  arr[Math.min(arr.length - 1, Math.floor(p * arr.length))] ?? 0;
const withRc = history.filter((h) => (h.summary.report?.confidence ?? 0) > 0);
const services = ["payment-service", "order-service", "user-service", "inventory-service"];
const bySvc = services.map((svc) => {
  const items = history.filter((h) => h.summary.incident.service === svc);
  const counts = new Map<string, number>();
  for (const h of items) {
    const label = byId.get(h.scenario)?.rootCauseLabel;
    if (label && h.summary.status === "completed") counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  const top = [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? null;
  return { service: svc, investigations: items.length, top_root_cause: top };
});
const benign = new Set([
  "healthy",
  "no_anomaly",
  "no_active_alerts",
  "no_recent_changes",
  "no_relevant_docs",
  "runbook_found",
  "mitigation_available",
  "known_issue_documented",
]);
const sigCount = new Map<string, number>();
for (const h of history)
  for (const sig of h.signals)
    if (!benign.has(sig)) sigCount.set(sig, (sigCount.get(sig) ?? 0) + 1);
const topSignals = [...sigCount.entries()]
  .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  .slice(0, 8)
  .map(([signal, count]) => ({ signal, count }));

const AGENT_META: Record<
  string,
  { description: string; capabilities: string[]; failRate: number }
> = {
  logs: {
    description: "Searches logs, clusters error patterns and extracts trace IDs (UC-01/02).",
    capabilities: ["logs"],
    failRate: 0.02,
  },
  metrics: {
    description:
      "Error rate, latency and saturation vs. the baseline, with anomaly windows (UC-05).",
    capabilities: ["metrics"],
    failRate: 0.03,
  },
  alerts: {
    description: "Firing and recently resolved alerts, silences and runbook annotations (UC-06).",
    capabilities: ["alerts"],
    failRate: 0.0,
  },
  k8s: {
    description:
      "Pods, restarts, events and rollout history via a read-only ServiceAccount (UC-07).",
    capabilities: ["k8s"],
    failRate: 0.02,
  },
  code: {
    description: "Recent commits, releases and risky config diffs (UC-08).",
    capabilities: ["code"],
    failRate: 0.0,
  },
  tickets: {
    description: "Open incidents, known issues and similar past tickets (UC-03).",
    capabilities: ["tickets"],
    failRate: 0.05,
  },
  knowledge: {
    description: "Runbooks and known issues via full-text search, no models (UC-09).",
    capabilities: ["knowledge"],
    failRate: 0.0,
  },
};
const agentStats = Object.entries(AGENT_META).map(([name, meta]) => {
  const runs = history
    .filter((h) => h.scenario !== "clarify")
    .reduce(
      (n, h) => n + (byId.get(h.scenario)?.agents.filter((a) => a.agent === name).length ?? 0),
      0,
    );
  const durations = SCENARIOS.flatMap((s) =>
    s.agents.filter((a) => a.agent === name).map((a) => a.duration * TIME_SCALE),
  ).sort((a, b) => a - b);
  return {
    name,
    runs,
    success_rate: round(1 - meta.failRate, 2),
    p50_ms: pct(durations, 0.5),
    tokens: 0,
  };
});

write("dashboard.json", DashboardSummary, {
  window_days: days,
  totals: {
    investigations: history.length,
    open: history.filter((h) =>
      ["running", "pending", "needs_clarification"].includes(h.summary.status),
    ).length,
    root_cause_found: withRc.length,
    no_incident: history.filter((h) => h.scenario === "S0" && h.summary.status !== "failed").length,
    failed: history.filter((h) => h.summary.status === "failed").length,
  },
  mttr_minutes: { p50: round(pct(mttr, 0.5), 1), p90: round(pct(mttr, 0.9), 1) },
  avg_confidence: round(
    withRc.reduce((n, h) => n + (h.summary.report?.confidence ?? 0), 0) / withRc.length,
    2,
  ),
  by_day: byDay,
  by_service: bySvc,
  top_signals: topSignals,
  agents: agentStats,
  recent: history.slice(0, 5).map((h) => h.summary),
});

// ---- 4. Catalogs ----------------------------------------------------------------------------
write("agents.json", AgentList, [
  ...agentStats.map((a) => ({
    name: a.name,
    version: "1.0.0",
    description: AGENT_META[a.name]!.description,
    capabilities: AGENT_META[a.name]!.capabilities,
    last_run_at: history[1]?.summary.created_at ?? null,
    success_rate_7d: a.success_rate,
    p50_ms: a.p50_ms,
  })),
  {
    name: "rca",
    version: "1.0.0",
    description:
      "Correlates every agent's findings, ranks hypotheses and cites evidence ids (UC-10).",
    capabilities: ["llm"],
    last_run_at: history[1]?.summary.created_at ?? null,
    success_rate_7d: 0.98,
    p50_ms: 7200,
  },
]);

write("services.json", ServiceList, [
  {
    name: "payment-service",
    description: "Processes card payments for orders.",
    owners: { team: "payments", slack: "#payments-oncall" },
    depends_on: ["postgres", "redis", "user-service"],
    environments: ["production", "staging"],
    runbooks: ["database-connection-pool.md", "payment-service.md"],
  },
  {
    name: "order-service",
    description: "Creates and tracks customer orders.",
    owners: { team: "commerce", slack: "#commerce-oncall" },
    depends_on: ["payment-service", "inventory-service", "postgres"],
    environments: ["production"],
    runbooks: ["order-service.md", "dependency-timeouts.md"],
  },
  {
    name: "user-service",
    description: "User accounts, authentication and profiles.",
    owners: { team: "identity", slack: "#identity-oncall" },
    depends_on: ["postgres", "redis"],
    environments: ["production"],
    runbooks: ["user-service.md"],
  },
  {
    name: "inventory-service",
    description: "Stock levels and reservations.",
    owners: { team: "commerce", slack: "#commerce-oncall" },
    depends_on: ["postgres"],
    environments: ["production"],
    runbooks: ["inventory-service.md"],
  },
]);

write(
  "scenarios.json",
  ScenarioList,
  SCENARIOS.filter((s) => s.id !== "S0").map((s) => ({
    id: s.id,
    title: s.title,
    service: s.service,
    description: s.description,
    active: false,
  })),
);

write("health.json", Health, {
  status: "ok",
  version: "0.6.0-demo",
  profile: "demo",
  llm: { provider: "fake", configured: false },
  capabilities: {
    logs: "ok",
    metrics: "ok",
    alerts: "ok",
    k8s: "ok",
    code: "ok",
    tickets: "ok",
    knowledge: "ok",
  },
  faults_enabled: false,
});

// ---- 5. Approvals ---------------------------------------------------------------------------
const s1 = templates.get("S1")!.investigation;
const s5 =
  history.find((h) => h.scenario === "S5" && h.summary.id !== "inv-demo-s5")?.summary ??
  history[0]!.summary;
const s2 = history.find((h) => h.scenario === "S2")?.summary ?? history[0]!.summary;
const jiraArgs = (inv: Investigation) => ({
  project: "OPS",
  issue_type: "Bug",
  priority: inv.report?.severity === "critical" ? "Highest" : "High",
  summary: `[RCA] ${inv.incident.service}: ${inv.report?.summary.split(".")[0] ?? inv.incident.title}`,
  labels: ["aiops", "rca", inv.incident.service ?? "unknown"],
  components: ["payments"],
  description: inv.report?.markdown ?? "",
});
write("approvals.json", ApprovalList, [
  {
    id: "apr-7f3a21",
    action: "create_ticket",
    capability: "tickets",
    tool: "jira_create_issue",
    arguments: jiraArgs(s1),
    reason: `Create a Jira ticket with the RCA of ${s1.id} (requested from the report view).`,
    risk: "low",
    status: "pending",
    requested_by: "vignesh",
    investigation_id: s1.id,
    created_at: iso(at(s1.completed_at!) + 40 * SEC),
    decided_by: null,
    decided_at: null,
    comment: null,
    result: null,
  },
  {
    id: "apr-6c2104",
    action: "create_ticket",
    capability: "tickets",
    tool: "jira_create_issue",
    arguments: {
      project: "OPS",
      issue_type: "Bug",
      priority: "High",
      summary: "[RCA] payment-service: Redis outage slows all services",
      labels: ["aiops", "rca"],
    },
    reason: `Create a Jira ticket with the RCA of ${s5.id}.`,
    risk: "low",
    status: "executed",
    requested_by: "vignesh",
    investigation_id: s5.id,
    created_at: iso(at(s5.completed_at!) + MIN),
    decided_by: "vignesh",
    decided_at: iso(at(s5.completed_at!) + 3 * MIN),
    comment: "LGTM",
    result: { key: "OPS-44", url: "http://localhost:8109/browse/OPS-44" },
  },
  {
    id: "apr-5b9007",
    action: "create_ticket",
    capability: "tickets",
    tool: "jira_create_issue",
    arguments: {
      project: "OPS",
      issue_type: "Bug",
      priority: "High",
      summary: "[RCA] order-service: OOMKilled restarts",
      labels: ["aiops", "rca"],
    },
    reason: `Create a Jira ticket with the RCA of ${s2.id}.`,
    risk: "low",
    status: "denied",
    requested_by: "vignesh",
    investigation_id: s2.id,
    created_at: iso(at(s2.completed_at!) + MIN),
    decided_by: "vignesh",
    decided_at: iso(at(s2.completed_at!) + 2 * MIN),
    comment: "Duplicate of OPS-21",
    result: null,
  },
]);

write(
  "meta.json",
  z.object({ anchor: z.string(), generated_by: z.string(), speed_hint: z.number() }),
  {
    anchor: iso(ANCHOR),
    generated_by: "frontend/scripts/generate-demo.ts",
    speed_hint: 4,
  },
);
