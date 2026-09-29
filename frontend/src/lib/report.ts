import type { EvidenceKind, Hypothesis, Investigation, TimelineEvent } from "@/lib/api/schemas";

/**
 * How the report hero reads an investigation (live-demo regression: a PARTIAL run with severity
 * High, 51 errors and "no root cause identified" was headed "NO INCIDENT DETECTED" in green).
 *
 * - `root_cause`: the report names a root-cause hypothesis.
 * - `healthy`: a true healthy result only: no abnormal signals (severity none), no hypothesis,
 *   and every agent ran (a partial run can't vouch that nothing is wrong).
 * - `no_root_cause`: everything else: abnormal signals (or a gap in the evidence) but no root
 *   cause; the best hypothesis is at most a weak *lead*.
 */
export type ReportOutcome =
  | { kind: "root_cause"; top: Hypothesis }
  | { kind: "healthy" }
  | { kind: "no_root_cause"; lead: Hypothesis | null; failedAgents: string[]; headline: string };

export function reportOutcome(inv: Investigation): ReportOutcome | null {
  const r = inv.report;
  if (!r) return null;
  const top = inv.hypotheses.find((h) => h.id === r.root_cause_hypothesis_id);
  if (top) return { kind: "root_cause", top };
  const failedAgents = [
    ...new Set(inv.results.filter((x) => x.status === "failed").map((x) => x.agent)),
  ];
  const healthy =
    r.severity === "none" &&
    inv.hypotheses.length === 0 &&
    failedAgents.length === 0 &&
    inv.status !== "partial";
  if (healthy) return { kind: "healthy" };
  const lead = [...inv.hypotheses].sort((a, b) => b.confidence - a.confidence)[0] ?? null;
  // the backend appends "Weak lead: <statement>" to the summary; the hero shows the lead apart
  const headline = r.summary.replace(/\s*Weak lead:[\s\S]*$/, "").trim() || r.summary;
  return { kind: "no_root_cause", lead, failedAgents, headline };
}

/** Evidence kinds that are context for an incident, not events of it. */
const CONTEXT_KINDS: ReadonlySet<EvidenceKind> = new Set<EvidenceKind>(["ticket", "doc"]);
const CONTEXT_AGENTS = new Set(["tickets", "knowledge"]);

/**
 * "Why it happened": the incident's own events first, in time order (changes, rollouts,
 * anomalies, alerts); related tickets/runbooks apart, so old tickets from earlier runs (the live
 * demo showed OPS-17..OPS-24 on top) never push the real chain down. Works on stored
 * investigations from before the backend ordered the timeline itself.
 */
export function splitTimeline(
  timeline: TimelineEvent[],
  kindOf: (evidenceId: string) => EvidenceKind | undefined,
): { chain: TimelineEvent[]; related: TimelineEvent[] } {
  const isContext = (t: TimelineEvent) => {
    const kind = t.evidence_id ? kindOf(t.evidence_id) : undefined;
    return kind ? CONTEXT_KINDS.has(kind) : CONTEXT_AGENTS.has(t.source.split(/[:.]/)[0] ?? "");
  };
  const chain = timeline
    .map((t, i) => ({ t, i }))
    .filter(({ t }) => !isContext(t))
    // stable: at equal times keep the backend's cause-before-effect order
    .sort((a, b) => Date.parse(a.t.timestamp) - Date.parse(b.t.timestamp) || a.i - b.i)
    .map(({ t }) => t);
  return { chain, related: timeline.filter(isContext) };
}

/** "Related tickets (3)", or "Related tickets and runbooks (3)" when both are present. */
export function relatedLabel(
  related: TimelineEvent[],
  kindOf: (evidenceId: string) => EvidenceKind | undefined,
): string {
  const isDoc = (t: TimelineEvent) =>
    t.evidence_id ? kindOf(t.evidence_id) === "doc" : t.source.startsWith("knowledge");
  const docs = related.filter(isDoc).length;
  const what =
    docs === 0 ? "tickets" : docs === related.length ? "runbooks" : "tickets and runbooks";
  return `Related ${what} (${related.length})`;
}
