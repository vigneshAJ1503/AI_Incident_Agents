/**
 * Serves the static demo dataset (src/demo/*.json) through the same interface as HttpClient and
 * simulates live SSE by replaying recorded event streams on a real-time schedule.
 * State (new investigations, approvals) lives in memory and sessionStorage: nothing leaves the tab.
 */
import { z } from "zod";

import agentsJson from "@/demo/agents.json";
import approvalsJson from "@/demo/approvals.json";
import dashboardJson from "@/demo/dashboard.json";
import healthJson from "@/demo/health.json";
import integrationsJson from "@/demo/integrations.json";
import investigationsJson from "@/demo/investigations.json";
import metaJson from "@/demo/meta.json";
import scenariosJson from "@/demo/scenarios.json";
import servicesJson from "@/demo/services.json";
import { classify, hasSymptoms } from "@/lib/ask/intent";
import {
  catalogAnswer,
  healthAnswer,
  helpAnswer,
  noMatchAnswer,
  recentAnswer,
  runningAnswer,
  runningItem,
} from "@/lib/demo/ask";
import { applyDemoUpdate, demoTestResult } from "@/lib/demo/integrations";
import { routeQuestion, type DemoScenarioId } from "@/lib/demo/route";
import {
  scheduleSession,
  sessionInvestigation,
  type DemoSession,
  type Recording,
} from "@/lib/demo/schedule";
import { dayShift, shiftTimes } from "@/lib/demo/time";

import {
  ApiError,
  type ApiClient,
  type Decision,
  type InvestigationFilters,
  type StreamHandlers,
} from "./client";
import { parseOrThrow } from "./parse";
import {
  AgentList,
  ApprovalList,
  DashboardSummary,
  Health,
  IntegrationList,
  Investigation,
  InvestigationSummary,
  LiveEvent,
  ScenarioList,
  ServiceList,
  type Approval,
  type AskAnswer,
  type AskRequest,
  type AskResponse,
  type CreateInvestigationRequest,
  type IntegrationUpdate,
  type InvestigationSummary as Summary,
} from "./schemas";

const RECORDINGS: Record<DemoScenarioId, () => Promise<{ default: unknown }>> = {
  S0: () => import("@/demo/scenario-S0.json"),
  S1: () => import("@/demo/scenario-S1.json"),
  S2: () => import("@/demo/scenario-S2.json"),
  S3: () => import("@/demo/scenario-S3.json"),
  S4: () => import("@/demo/scenario-S4.json"),
  S5: () => import("@/demo/scenario-S5.json"),
};
const Rec = z.object({ investigation: Investigation, events: z.array(LiveEvent) });
const History = z.object({
  items: z.array(InvestigationSummary),
  scenario_of: z.record(z.string(), z.string()),
});
const STORE_KEY = "aiops-demo-state-v1";

interface Persisted {
  sessions: DemoSession[];
  approvals: Approval[];
  active: string | null;
  /** Settings → Integrations edits (PR-046): never a secret value, only its last 4 chars. */
  integrations?: unknown;
}

export interface DemoClientOptions {
  speed: number;
  now?: () => number;
  /** Simulated network latency for REST calls (ms). */
  latency?: number;
  storage?: Pick<Storage, "getItem" | "setItem"> | null;
}

export class DemoClient implements ApiClient {
  readonly kind = "demo" as const;
  private readonly speed: number;
  private readonly now: () => number;
  private readonly latency: number;
  private readonly storage: Pick<Storage, "getItem" | "setItem"> | null;
  private readonly shift: number;
  private readonly history: z.infer<typeof History>;
  private readonly recordings = new Map<string, Recording>();
  private sessions = new Map<string, DemoSession>();
  private approvalState: Approval[];
  private activeScenario: string | null = null;
  private integrationState: IntegrationList;
  private listeners = new Map<string, Set<() => void>>();

  constructor(opts: DemoClientOptions) {
    this.speed = opts.speed;
    this.now = opts.now ?? Date.now;
    this.latency = opts.latency ?? 120;
    this.storage =
      opts.storage === undefined
        ? typeof sessionStorage === "undefined"
          ? null
          : sessionStorage
        : opts.storage;
    this.shift = dayShift(metaJson.anchor, this.now());
    this.history = parseOrThrow(
      History,
      shiftTimes(investigationsJson, this.shift),
      "demo investigations.json",
    );
    this.approvalState = parseOrThrow(
      ApprovalList,
      shiftTimes(approvalsJson, this.shift),
      "demo approvals.json",
    );
    this.integrationState = parseOrThrow(
      IntegrationList,
      integrationsJson,
      "demo integrations.json",
    );
    // the built-in open investigation waiting for a clarification (UC-13)
    const open = this.history.items.find((i) => i.status === "needs_clarification");
    if (open) {
      this.sessions.set(open.id, {
        id: open.id,
        incidentId: open.incident.id,
        question: open.incident.title,
        route: {
          scenario: "clarify",
          candidates: ["payment-service", "order-service", "user-service"],
        },
        createdAt: Date.parse(open.created_at),
      });
    }
    this.load();
  }

  // ---- persistence -----------------------------------------------------------------------------
  private load(): void {
    try {
      const raw = this.storage?.getItem(STORE_KEY);
      if (!raw) return;
      const p = JSON.parse(raw) as Persisted;
      for (const s of p.sessions) this.sessions.set(s.id, s);
      this.approvalState = parseOrThrow(ApprovalList, p.approvals, "demo state");
      this.activeScenario = p.active;
      if (p.integrations)
        this.integrationState = parseOrThrow(IntegrationList, p.integrations, "demo state");
    } catch {
      // corrupt or blocked storage: start fresh
    }
  }
  private save(): void {
    try {
      const p: Persisted = {
        sessions: [...this.sessions.values()],
        approvals: this.approvalState,
        active: this.activeScenario,
        integrations: this.integrationState,
      };
      this.storage?.setItem(STORE_KEY, JSON.stringify(p));
    } catch {
      // storage full or blocked: the demo still works for this tab
    }
  }
  private notify(id: string): void {
    for (const fn of this.listeners.get(id) ?? []) fn();
  }

  private delay<T>(value: T): Promise<T> {
    return new Promise((resolve) => setTimeout(() => resolve(value), this.latency));
  }

  private async recording(id: DemoScenarioId): Promise<Recording> {
    const hit = this.recordings.get(id);
    if (hit) return hit;
    const mod = await RECORDINGS[id]();
    const rec = parseOrThrow(Rec, mod.default, `demo scenario-${id}.json`);
    this.recordings.set(id, rec);
    return rec;
  }

  private sessionScenario(s: DemoSession): DemoScenarioId | null {
    if (s.resolved) return s.resolved.scenario;
    return s.route.scenario === "clarify" ? null : s.route.scenario;
  }

  private sessionSummary(s: DemoSession, inv: Investigation): Summary {
    return {
      id: s.id,
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
      mode: "demo",
    };
  }

  private async allSummaries(): Promise<Summary[]> {
    const fromSessions: Summary[] = [];
    for (const s of this.sessions.values()) {
      fromSessions.push(this.sessionSummary(s, await this.investigationOfSession(s)));
    }
    const ids = new Set(fromSessions.map((x) => x.id));
    return [...fromSessions, ...this.history.items.filter((i) => !ids.has(i.id))].sort((a, b) =>
      b.created_at.localeCompare(a.created_at),
    );
  }

  private async investigationOfSession(s: DemoSession): Promise<Investigation> {
    const sc = this.sessionScenario(s);
    const rec = sc ? await this.recording(sc) : null;
    return sessionInvestigation(s, rec, this.speed, this.now());
  }

  // ---- ApiClient ----------------------------------------------------------------------------------
  health() {
    return this.delay(parseOrThrow(Health, healthJson, "demo health.json"));
  }
  services() {
    return this.delay(parseOrThrow(ServiceList, servicesJson, "demo services.json"));
  }
  agents() {
    return this.delay(
      parseOrThrow(AgentList, shiftTimes(agentsJson, this.shift), "demo agents.json"),
    );
  }
  async dashboard() {
    const d = parseOrThrow(
      DashboardSummary,
      shiftTimes(dashboardJson, this.shift),
      "demo dashboard.json",
    );
    const all = await this.allSummaries();
    const extra = all.length - this.history.items.length;
    return this.delay({
      ...d,
      totals: {
        ...d.totals,
        investigations: d.totals.investigations + extra,
        open: all.filter((x) => ["running", "pending", "needs_clarification"].includes(x.status))
          .length,
      },
      recent: all.slice(0, 5),
    });
  }
  async listInvestigations(f: InvestigationFilters = {}) {
    const q = f.q?.trim().toLowerCase();
    const items = (await this.allSummaries()).filter(
      (i) =>
        (!f.status || i.status === f.status) &&
        (!f.service ||
          i.incident.service === f.service ||
          i.affected_services.includes(f.service)) &&
        (!f.severity || i.report?.severity === f.severity) &&
        (!q ||
          `${i.id} ${i.incident.id} ${i.incident.title} ${i.report?.summary ?? ""}`
            .toLowerCase()
            .includes(q)),
    );
    const limit = f.limit ?? 50;
    const offset = Number(f.cursor ?? 0) || 0;
    const page = items.slice(offset, offset + limit);
    return this.delay({
      items: page,
      next_cursor: offset + limit < items.length ? String(offset + limit) : null,
    });
  }

  async getInvestigation(id: string): Promise<Investigation> {
    const s = this.sessions.get(id);
    if (s) return this.delay(await this.investigationOfSession(s));
    const sc = this.history.scenario_of[id] as DemoScenarioId | undefined;
    const summary = this.history.items.find((i) => i.id === id);
    if (!sc || !summary) throw new ApiError(`Investigation ${id} not found`, 404, "not_found");
    const rec = await this.recording(sc);
    const inv = shiftTimes(
      rec.investigation,
      Date.parse(summary.created_at) - Date.parse(rec.investigation.created_at),
    );
    const merged: Investigation = {
      ...inv,
      id,
      incident: summary.incident,
      status: summary.status,
      created_at: summary.created_at,
      completed_at: summary.completed_at ?? null,
      duration_ms: summary.duration_ms ?? inv.duration_ms,
    };
    if (summary.status === "failed") {
      return this.delay({
        ...merged,
        report: null,
        hypotheses: [],
        recommendations: [],
        results: merged.results.slice(0, 3).map((r, i) =>
          i === 2
            ? {
                ...r,
                status: "failed" as const,
                summary: "Tool call timed out after 30 s",
                error: "timeout",
                evidence: [],
                findings: [],
              }
            : r,
        ),
        steps: merged.steps.map((st, i) =>
          i > 2
            ? { ...st, status: "skipped" as const }
            : i === 2
              ? { ...st, status: "failed" as const }
              : st,
        ),
      });
    }
    if (merged.report && summary.report) {
      merged.report = {
        ...merged.report,
        confidence: summary.report.confidence,
        severity: summary.report.severity,
      };
    }
    return this.delay(merged);
  }

  async createInvestigation(req: CreateInvestigationRequest) {
    const now = this.now();
    const id = `inv-${now.toString(16).slice(-8)}${Math.floor(Math.random() * 0xfff)
      .toString(16)
      .padStart(3, "0")}`;
    const route = routeQuestion(req.question, req.service);
    const s: DemoSession = {
      id,
      incidentId: `INC-${2000 + this.sessions.size}`,
      question: req.question.trim(),
      route,
      createdAt: now,
    };
    this.sessions.set(id, s);
    this.save();
    return this.delay({ id, status: "pending" as const });
  }

  async ask(req: AskRequest): Promise<AskResponse> {
    const c = classify(req.question);
    const base = {
      intent: c.intent,
      confidence: c.sure ? 0.9 : 0.5,
      source: c.sure ? ("rules" as const) : ("default" as const),
    };
    if (c.kind === "platform") {
      let answer: AskAnswer;
      if (c.intent === "agents_running") {
        const now = this.now();
        const running = [];
        for (const s of this.sessions.values()) {
          const sc = this.sessionScenario(s);
          const item = runningItem(
            s,
            scheduleSession(s, sc ? await this.recording(sc) : null, this.speed),
            now,
          );
          if (item) running.push(item);
        }
        running.sort((a, b) => b.created_at.localeCompare(a.created_at));
        answer = runningAnswer(running, await this.allSummaries());
      } else if (c.intent === "agents_catalog") answer = catalogAnswer(await this.agents());
      else if (c.intent === "health") answer = healthAnswer(await this.health());
      else if (c.intent === "recent_investigations")
        answer = recentAnswer(await this.allSummaries(), c.service, req.question);
      else answer = helpAnswer();
      return this.delay({ kind: "platform", ...base, answer });
    }
    // replay semantics: only a question about a recorded scenario's service/symptoms runs
    if (!req.service && !c.service && !hasSymptoms(req.question)) {
      return this.delay({ kind: "incident", ...base, answer: noMatchAnswer() });
    }
    const created = await this.createInvestigation({
      question: req.question,
      ...(req.service ? { service: req.service } : {}),
    });
    const route = this.sessions.get(created.id)?.route;
    return {
      kind: "incident",
      ...base,
      investigation_id: created.id,
      mode: "replay",
      scenario: route && route.scenario !== "clarify" ? route.scenario : null,
    };
  }

  async clarify(id: string, answer: string) {
    const s = this.sessions.get(id);
    if (!s || s.route.scenario !== "clarify")
      throw new ApiError("This investigation is not waiting for a clarification", 409, "conflict");
    const r = routeQuestion(`${s.question} ${answer}`, answer.match(/[a-z]+-service/)?.[0] ?? null);
    const resolved =
      r.scenario === "clarify" ? { scenario: "S1" as const, service: "payment-service" } : r;
    this.sessions.set(id, { ...s, clarifiedAt: this.now(), resolved });
    this.save();
    this.notify(id);
    await this.delay(undefined);
  }

  async cancel(id: string) {
    const s = this.sessions.get(id);
    if (!s) throw new ApiError("Only running investigations can be cancelled", 409, "conflict");
    this.sessions.set(id, { ...s, cancelledAt: this.now() });
    this.save();
    this.notify(id);
    await this.delay(undefined);
  }

  async reportMarkdown(id: string) {
    const inv = await this.getInvestigation(id);
    if (!inv.report) throw new ApiError("The report is not ready yet", 409, "conflict");
    return inv.report.markdown;
  }

  async draftTicket(id: string) {
    const existing = this.approvalState.find(
      (a) => a.investigation_id === id && a.status === "pending",
    );
    if (existing) return this.delay({ approval_id: existing.id });
    const inv = await this.getInvestigation(id);
    if (!inv.report) throw new ApiError("The report is not ready yet", 409, "conflict");
    const approval: Approval = {
      id: `apr-${this.now().toString(16).slice(-6)}`,
      action: "create_ticket",
      capability: "tickets",
      tool: "jira_create_issue",
      arguments: {
        project: "OPS",
        issue_type: "Bug",
        priority: inv.report.severity === "critical" ? "Highest" : "High",
        summary: `[RCA] ${inv.incident.service ?? "unknown"}: ${inv.report.summary.split(".")[0]}`,
        labels: ["aiops", "rca", inv.incident.service ?? "unknown"],
        description: inv.report.markdown,
      },
      reason: `Create a Jira ticket with the RCA of ${id} (requested from the report view).`,
      risk: "low",
      status: "pending",
      requested_by: "you",
      investigation_id: id,
      created_at: new Date(this.now()).toISOString(),
      decided_by: null,
      decided_at: null,
      comment: null,
      result: null,
    };
    this.approvalState = [approval, ...this.approvalState];
    this.save();
    return this.delay({ approval_id: approval.id });
  }

  approvals(status?: string) {
    return this.delay(this.approvalState.filter((a) => !status || a.status === status));
  }

  async decide(id: string, decision: Decision, body: { by: string; comment?: string }) {
    const a = this.approvalState.find((x) => x.id === id);
    if (!a) throw new ApiError(`Approval ${id} not found`, 404, "not_found");
    if (a.status !== "pending")
      throw new ApiError(`Approval ${id} is already ${a.status}`, 409, "conflict");
    const n = 45 + this.approvalState.filter((x) => x.status === "executed").length;
    const updated: Approval = {
      ...a,
      status: decision === "approve" ? "executed" : "denied",
      decided_by: body.by,
      decided_at: new Date(this.now()).toISOString(),
      comment: body.comment ?? null,
      result:
        decision === "approve"
          ? { key: `OPS-${n}`, url: `http://localhost:8109/browse/OPS-${n}` }
          : null,
    };
    this.approvalState = this.approvalState.map((x) => (x.id === id ? updated : x));
    this.save();
    return new Promise<Approval>((resolve) => setTimeout(() => resolve(updated), this.latency * 4));
  }

  scenarios() {
    const list = parseOrThrow(ScenarioList, scenariosJson, "demo scenarios.json");
    return this.delay(list.map((s) => ({ ...s, active: s.id === this.activeScenario })));
  }
  async injectScenario() {
    await this.delay(undefined);
    throw new ApiError(
      "Fault injection is disabled in demo mode (AIOPS_ENABLE_FAULTS=0)",
      403,
      "forbidden",
    );
  }
  async revertScenarios() {
    await this.delay(undefined);
    throw new ApiError(
      "Fault injection is disabled in demo mode (AIOPS_ENABLE_FAULTS=0)",
      403,
      "forbidden",
    );
  }

  integrations() {
    return this.delay(structuredClone(this.integrationState));
  }
  private integration(capability: string) {
    const item = this.integrationState.items.find((i) => i.capability === capability);
    if (!item) throw new ApiError(`Capability '${capability}' not found`, 404, "not_found");
    return item;
  }
  async saveIntegration(capability: string, update: IntegrationUpdate) {
    await this.delay(undefined);
    const saved = applyDemoUpdate(
      this.integration(capability),
      update,
      "demo-operator",
      new Date(this.now()).toISOString(),
      this.integrationState.secrets_enabled,
    );
    this.integrationState = {
      ...this.integrationState,
      items: this.integrationState.items.map((i) =>
        i.capability === capability ? saved.integration : i,
      ),
    };
    this.save();
    return structuredClone(saved);
  }
  async testIntegration(capability: string, draft?: IntegrationUpdate) {
    const item = this.integration(capability);
    await this.delay(undefined);
    return new Promise<ReturnType<typeof demoTestResult>>((resolve) =>
      setTimeout(() => resolve(demoTestResult(item, draft)), this.latency * 4),
    );
  }

  subscribe(id: string, h: StreamHandlers, lastSeq = 0): () => void {
    let stopped = false;
    let timers: ReturnType<typeof setTimeout>[] = [];
    let delivered = lastSeq;
    const clear = () => {
      timers.forEach(clearTimeout);
      timers = [];
    };

    const plan = async () => {
      clear();
      const s = this.sessions.get(id);
      let schedule: { event: LiveEvent; due: number }[];
      if (s) {
        const sc = this.sessionScenario(s);
        schedule = scheduleSession(s, sc ? await this.recording(sc) : null, this.speed);
      } else {
        // a finished investigation: replay its past events at once
        const sc = this.history.scenario_of[id] as DemoScenarioId | undefined;
        if (!sc) {
          h.onError?.(new ApiError(`Investigation ${id} not found`, 404, "not_found"));
          return;
        }
        const rec = await this.recording(sc);
        schedule = rec.events.map((e) => ({ event: { ...e, investigation_id: id }, due: 0 }));
      }
      if (stopped) return;
      h.onState?.("open");
      const now = this.now();
      for (const item of schedule) {
        if (item.event.seq <= delivered) continue;
        const fire = () => {
          if (stopped || item.event.seq <= delivered) return;
          delivered = item.event.seq;
          h.onEvent(item.event);
          if (item.event.type === "investigation_finished") {
            h.onState?.("closed");
          }
        };
        if (item.due <= now) fire();
        else timers.push(setTimeout(fire, item.due - now));
      }
    };

    h.onState?.("connecting");
    const listener = () => void plan();
    const set = this.listeners.get(id) ?? new Set();
    set.add(listener);
    this.listeners.set(id, set);
    void plan();
    return () => {
      stopped = true;
      clear();
      set.delete(listener);
    };
  }
}
