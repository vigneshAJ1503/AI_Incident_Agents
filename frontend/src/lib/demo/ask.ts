/** Demo only: answers to platform questions (PR-041) built from the static dataset. */
import type {
  Agent,
  AskAnswer,
  AskItem,
  Health,
  InvestigationStatus,
  InvestigationSummary,
} from "@/lib/api/schemas";
import { SCENARIO_QUESTIONS, TRY_ASKING } from "@/lib/ask/examples";

import type { DemoSession, Scheduled } from "./schedule";

/** The demo profile's provider behind each capability (profiles/local). */
export const DEMO_PROVIDERS: Record<string, string> = {
  logs: "elasticsearch",
  metrics: "prometheus",
  alerts: "alertmanager",
  k8s: "kubernetes",
  code: "git",
  tickets: "mock",
  knowledge: "postgres_fts",
};

type Running = Extract<AskItem, { type: "running_investigation" }>;
type InvItem = Extract<AskItem, { type: "investigation" }>;

const href = (id: string) => `/investigations/${id}`;

/** A simulated run in flight at `now` (started, not finished, not waiting for an answer). */
export function runningItem(s: DemoSession, schedule: Scheduled[], now: number): Running | null {
  const due = schedule.filter((x) => x.due <= now).map((x) => x.event);
  if (!due.length || due.some((e) => e.type === "investigation_finished")) return null;
  if (due.at(-1)?.type === "clarification_needed") return null;
  const steps = new Map<string, { agent: string; round: number; status: string; tool?: string }>();
  let round: number | null = null;
  for (const e of due) {
    if (e.type === "plan_created")
      for (const st of e.data.steps)
        steps.set(st.id, { agent: st.agent, round: st.round, status: "queued" });
    else if (e.type === "round_started") round = e.data.round;
    else if (e.type === "agent_started") {
      const st = steps.get(e.data.step_id);
      if (st) st.status = "running";
    } else if (e.type === "agent_finished") {
      const st = steps.get(e.data.step_id);
      if (st) st.status = e.data.status === "failed" ? "failed" : "done";
    } else if (e.type === "tool_called") {
      const st = steps.get(e.data.step_id);
      if (st) st.tool = e.data.tool;
    }
  }
  return {
    type: "running_investigation",
    id: s.id,
    question: s.question,
    service: s.resolved?.service ?? (s.route.scenario === "clarify" ? null : s.route.service),
    status: "running",
    mode: "demo",
    created_at: new Date(s.createdAt).toISOString(),
    elapsed_s: Math.max(0, Math.round((now - s.createdAt) / 100) / 10),
    round,
    agents: [...steps.values()].map((st) => ({
      agent: st.agent,
      round: st.round,
      status: st.status,
      tool: st.tool ?? null,
    })),
    href: href(s.id),
  };
}

export function investigationItem(i: InvestigationSummary): InvItem {
  return {
    type: "investigation",
    id: i.id,
    title: i.incident.title,
    service: i.incident.service ?? null,
    status: i.status,
    severity: i.report?.severity ?? null,
    root_cause: i.report?.summary ?? null,
    confidence: i.report?.confidence ?? null,
    created_at: i.created_at,
    href: href(i.id),
  };
}

export function runningAnswer(running: Running[], recent: InvestigationSummary[]): AskAnswer {
  if (running.length) {
    const lines = running.map((r) => {
      const active = r.agents.filter((a) => a.status === "running").map((a) => a.agent);
      const doing = active.length ? `agents working: ${active.join(", ")}` : "planning";
      return `- **${r.question}** (${r.service ?? "service not resolved yet"}): ${doing}, round ${r.round ?? "-"}, ${Math.floor(r.elapsed_s)} s so far`;
    });
    const n = running.length;
    return {
      title: `${n} investigation${n === 1 ? "" : "s"} running`,
      markdown: lines.join("\n"),
      items: running,
      links: [{ label: "Open live view", href: running[0]!.href }],
    };
  }
  return {
    title: "Nothing is running right now",
    markdown: "No investigation is running, so no agent is working. The last ones:",
    items: recent.slice(0, 3).map(investigationItem),
    links: [{ label: "All investigations", href: "/investigations" }],
  };
}

export function catalogAnswer(all: Agent[]): AskAnswer {
  // the RCA agent is part of the orchestrator, not a specialist (the API's registry agrees)
  const agents = all.filter((a) => a.name !== "rca");
  return {
    title: "The agents",
    markdown: `**${agents.length} specialist agents** check an incident in parallel; an RCA agent then ranks the hypotheses with evidence. Each agent binds to a capability, and the profile picks the provider behind it.`,
    items: agents.map((a) => ({
      type: "agent" as const,
      name: a.name,
      description: a.description,
      capabilities: a.capabilities,
      providers: a.capabilities.map((c) => DEMO_PROVIDERS[c] ?? c),
      enabled: true,
      success_rate_7d: a.success_rate_7d ?? null,
      p50_ms: a.p50_ms ?? null,
      runs_7d: 0,
    })),
    links: [{ label: "Agents page", href: "/agents" }],
  };
}

export function healthAnswer(h: Health): AskAnswer {
  const checks: AskItem[] = [
    {
      type: "check",
      name: "LLM",
      status: h.llm.configured ? "ok" : "down",
      detail: h.llm.configured
        ? `${h.llm.provider}: configured`
        : "not configured in demo mode (zero tokens: recorded scenarios)",
    },
    ...Object.entries(h.capabilities).map(([name, status]) => ({
      type: "check" as const,
      name,
      status,
      detail: `${DEMO_PROVIDERS[name] ?? name}: ${status === "ok" ? "reachable" : status}`,
    })),
    { type: "check", name: "fault injection", status: "info", detail: "disabled in demo mode" },
    { type: "check", name: "profile", status: "info", detail: h.profile },
  ];
  const down = checks.filter((c) => c.type === "check" && c.status === "down");
  return {
    title: "Platform health",
    markdown:
      (down.length
        ? `Down or not configured: **${down.map((c) => (c.type === "check" ? c.name : "")).join(", ")}**.`
        : "Everything is up.") +
      "\n\nThis is the static **demo**: new investigations replay the recorded scenarios.",
    items: checks,
    links: [{ label: "Dashboard", href: "/" }],
  };
}

export function helpAnswer(): AskAnswer {
  return {
    title: "What you can ask",
    markdown:
      "Ask about an **incident** and the agents investigate it. In demo mode I replay the recorded incidents below.\n\nOr ask about the **platform** itself (agents, health, past incidents).",
    items: [
      ...scenarioChips(),
      ...TRY_ASKING.map((q) => ({ type: "suggestion" as const, question: q, hint: "platform" })),
    ],
    links: [{ label: "Scenarios", href: "/scenarios" }],
  };
}

export function scenarioChips(): AskItem[] {
  return SCENARIO_QUESTIONS.map((s) => ({
    type: "suggestion" as const,
    question: s.q,
    hint: s.hint,
    scenario: s.scenario,
  }));
}

export function noMatchAnswer(): AskAnswer {
  return {
    title: "No recorded incident matches this question",
    markdown:
      "In demo/replay mode I can investigate these recorded incidents (click one to run it):",
    items: scenarioChips(),
    links: [{ label: "Scenarios", href: "/scenarios" }],
  };
}

const STATUSES: InvestigationStatus[] = ["failed", "completed", "cancelled", "partial", "running"];

export function recentAnswer(
  all: InvestigationSummary[],
  service: string | null,
  question: string,
): AskAnswer {
  const q = question.toLowerCase();
  const status = STATUSES.find((s) => q.includes(s));
  const severity = (["critical", "high", "medium", "low"] as const).find((s) => q.includes(s));
  const single = /\b(the last|the latest|most recent)\b/.test(q);
  const found = all
    .filter(
      (i) =>
        (!service || i.incident.service === service || i.affected_services.includes(service)) &&
        (!status || i.status === status) &&
        (!severity || i.report?.severity === severity),
    )
    .slice(0, single ? 1 : 5);
  const filters = [service, status, severity].filter(Boolean).join(", ");
  const title = `Recent investigations${filters ? ` (${filters})` : ""}`;
  return {
    title,
    markdown: found.length
      ? found
          .map(
            (i) =>
              `- **${i.incident.title}**: ${i.report?.summary ?? i.status}${i.report ? ` (${Math.round(i.report.confidence * 100)}% confidence)` : ""}`,
          )
          .join("\n")
      : `No investigation matches${filters ? ` ${filters}` : ""}.`,
    items: found.map(investigationItem),
    links: [
      {
        label: "Investigations",
        href: `/investigations${service ? `?service=${service}` : ""}`,
      },
    ],
  };
}
