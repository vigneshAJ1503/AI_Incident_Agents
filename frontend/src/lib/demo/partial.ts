import type { Evidence, Investigation, TimelineEvent } from "@/lib/api/schemas";

/** Agents that fail in the demo's partial run (like the live demo: the evidence has a gap). */
const FAILED = new Set(["metrics", "k8s"]);
const LEAD_CAP = 0.45;
/** Old tickets from earlier test runs, as the live demo's tickets agent found them. */
const OLD_TICKETS = [17, 18, 19, 20].map((n) => ({
  key: `OPS-${n}`,
  summary: `payment-service 500s during load test run ${n - 16}`,
}));

/**
 * The demo dataset's PARTIAL case: abnormal signals (severity stays), but two agents failed, so
 * the RCA has only a weak lead and no root cause. It also carries old tickets in the timeline
 * the way stored pre-fix investigations do, so the UI's ordering is exercised. Mirrors the
 * backend's partial report (`build_report`: summary, confidence 0, no root-cause id).
 */
export function partialOf(inv: Investigation, summary: string | undefined): Investigation {
  const failedEvidence = new Set(
    inv.results.filter((r) => FAILED.has(r.agent)).flatMap((r) => r.evidence.map((e) => e.id)),
  );
  const keep = (ids: string[]) => ids.filter((id) => !failedEvidence.has(id));
  const incidentStart = Date.parse(inv.context?.time_range.start ?? inv.created_at);
  const tickets: Evidence[] = OLD_TICKETS.map((t, i) => ({
    id: `ev-partial-ticket-${i + 1}`,
    kind: "ticket",
    source: "jira",
    summary: `${t.key} [Done, resolved] ${t.summary}`,
    timestamp: new Date(incidentStart - (20 - i) * 3_600_000).toISOString(),
    link: `http://localhost:8109/browse/${t.key}`,
    query: null,
    data: { key: t.key, status: "Done" },
  }));
  const ticketEvents: TimelineEvent[] = tickets.map((e) => ({
    timestamp: e.timestamp!,
    description: e.summary,
    source: "tickets:jira",
    evidence_id: e.id,
  }));
  const hypotheses = inv.hypotheses.map((h) => ({
    ...h,
    confidence: Math.min(LEAD_CAP, h.confidence * 0.5),
    supporting_evidence_ids: keep(h.supporting_evidence_ids),
    contradicting_evidence_ids: keep(h.contradicting_evidence_ids),
  }));
  const lead = hypotheses[0]?.statement ?? "none";
  const service = inv.context?.service ?? "the service";
  const env = inv.context?.environment ?? "the environment";
  return {
    ...inv,
    status: "partial",
    results: inv.results.map((r) =>
      FAILED.has(r.agent)
        ? {
            ...r,
            status: "failed" as const,
            summary: "Tool call timed out after 30 s",
            error: "timeout",
            evidence: [],
            findings: [],
            signals: [],
          }
        : r.agent === "tickets"
          ? { ...r, evidence: [...tickets, ...r.evidence] }
          : r,
    ),
    steps: inv.steps.map((s) => (FAILED.has(s.agent) ? { ...s, status: "failed" as const } : s)),
    hypotheses,
    claims: [],
    recommendations: inv.recommendations.map((r) => ({ ...r, evidence_ids: keep(r.evidence_ids) })),
    // old tickets first, as a pre-fix backend stored them (sorted by time only)
    timeline: [
      ...ticketEvents,
      ...inv.timeline.filter((t) => !t.evidence_id || !failedEvidence.has(t.evidence_id)),
    ],
    report: inv.report && {
      ...inv.report,
      summary:
        summary ??
        `Abnormal signals on ${service} in ${env} but no root cause identified: the evidence is too weak or inconsistent. Weak lead: ${lead}`,
      root_cause_hypothesis_id: null,
      confidence: 0,
      next_steps: [
        "Re-run the investigation once the failed agents (Metrics, Kubernetes) are reachable.",
        ...inv.report.next_steps,
      ],
      open_questions: [
        "Not checked / incomplete: metrics (timeout), k8s (timeout)",
        `Evidence is too weak for a root cause: which additional data would confirm '${lead}'?`,
      ],
    },
  };
}
