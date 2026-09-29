"use client";

import { motion } from "framer-motion";
import {
  BellRingIcon,
  BookOpenIcon,
  BoxesIcon,
  ChartLineIcon,
  ChevronRightIcon,
  CircleCheckBigIcon,
  FileQuestionIcon,
  FileTextIcon,
  GitCommitHorizontalIcon,
  ListChecksIcon,
  ScrollTextIcon,
  SearchAlertIcon,
  ShieldAlertIcon,
  TicketIcon,
  TriangleAlertIcon,
  type LucideIcon,
} from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import { ConfidenceRing } from "@/components/confidence-ring";
import { DeepLink, EvidenceBody } from "@/components/evidence/evidence-body";
import {
  Citation,
  ClaimBadge,
  EvidenceProvider,
  useEvidence,
} from "@/components/evidence/evidence-drawer";
import { itemVariants, listVariants } from "@/components/motion";
import { EmptyState } from "@/components/states";
import { SeverityBadge, StepBadge } from "@/components/status";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { Evidence, EvidenceKind, Investigation, TimelineEvent } from "@/lib/api/schemas";
import { collectEvidence } from "@/lib/evidence";
import {
  formatClock,
  formatDateTime,
  formatDuration,
  formatPercent,
  humanizeSignal,
} from "@/lib/format";
import { relatedLabel, reportOutcome, splitTimeline, type ReportOutcome } from "@/lib/report";
import { cn } from "@/lib/utils";

import { agentMeta } from "./agent-meta";
import { CostCard } from "./cost-card";
import { ReportActions } from "./report-actions";

type Ev = Evidence & { agent: string };
const NO_EVIDENCE: Ev[] = [];

const HERO_TONE = {
  root_cause: {
    border: "border-danger/30",
    glow: "from-danger/12",
    bar: "bg-danger",
    text: "text-danger",
    icon: ShieldAlertIcon,
    label: "Root cause identified",
  },
  no_root_cause: {
    border: "border-warn/40",
    glow: "from-warn/15",
    bar: "bg-warn",
    text: "text-warn",
    icon: SearchAlertIcon,
    label: "No root cause identified",
  },
  healthy: {
    border: "border-ok/40",
    glow: "from-ok/15",
    bar: "bg-ok",
    text: "text-ok",
    icon: CircleCheckBigIcon,
    label: "No incident detected",
  },
} as const;

function RootCauseHero({ inv }: { inv: Investigation }) {
  const r = inv.report!;
  const outcome = reportOutcome(inv)!;
  const top = outcome.kind === "root_cause" ? outcome.top : null;
  const tone = HERO_TONE[outcome.kind];
  return (
    <motion.section
      // slide only, no fade: the root-cause headline is the LCP element and must paint at once
      initial={{ y: 8 }}
      animate={{ y: 0 }}
      aria-labelledby="root-cause-title"
      data-testid="root-cause"
      data-outcome={outcome.kind}
      className={cn("relative overflow-hidden rounded-2xl glass p-6 shadow-elev-3", tone.border)}
    >
      <div
        aria-hidden
        className={cn(
          "pointer-events-none absolute -top-24 -right-24 size-72 bg-radial to-transparent to-70%",
          tone.glow,
        )}
      />
      <div aria-hidden className={cn("absolute inset-y-0 left-0 w-1.5", tone.bar)} />
      <div className="relative flex flex-col gap-6 md:flex-row md:items-center">
        <div className="min-w-0 flex-1 space-y-3">
          <p className="flex items-center gap-2 text-xs font-semibold tracking-wide uppercase">
            <tone.icon aria-hidden className={cn("size-4", tone.text)} />{" "}
            <span className={tone.text}>{tone.label}</span>
          </p>
          <h2 id="root-cause-title" className="text-lg leading-snug font-semibold md:text-xl">
            {top ? top.statement : outcome.kind === "no_root_cause" ? outcome.headline : r.summary}
          </h2>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {r.severity !== "none" && <SeverityBadge severity={r.severity} />}
            {r.affected_services.map((s) => (
              <Badge key={s} tone="outline" className="font-mono">
                {s}
              </Badge>
            ))}
          </div>
          {r.impact && (
            <p className="text-sm text-muted-foreground">
              <span className="font-medium text-foreground">Impact:</span> {r.impact}
            </p>
          )}
          {top && top.supporting_evidence_ids.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-muted-foreground">Evidence:</span>
              {top.supporting_evidence_ids.map((id) => (
                <Citation key={id} id={id} trail={top.supporting_evidence_ids} />
              ))}
            </div>
          )}
          {outcome.kind === "no_root_cause" && <WeakLead outcome={outcome} />}
        </div>
        {top && <ConfidenceRing value={r.confidence} className="shrink-0 self-center" />}
      </div>
    </motion.section>
  );
}

/** The best hypothesis when the evidence is too weak: labelled as a lead, never as the cause. */
function WeakLead({ outcome }: { outcome: Extract<ReportOutcome, { kind: "no_root_cause" }> }) {
  const { lead, failedAgents } = outcome;
  const failed = failedAgents.map((a) => agentMeta(a).label).join(", ");
  return (
    <div className="space-y-2" data-testid="weak-lead">
      {lead && (
        <div className="space-y-1.5 rounded-lg border border-dashed border-warn/50 p-3">
          <p className="flex flex-wrap items-center gap-2 text-xs">
            <Badge tone="warn">Weak lead · not confirmed</Badge>
            <span className="text-muted-foreground tabular-nums">
              {formatPercent(lead.confidence)} confidence
            </span>
          </p>
          <p className="text-sm">{lead.statement}</p>
          {lead.supporting_evidence_ids.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-muted-foreground">Evidence:</span>
              {lead.supporting_evidence_ids.map((id) => (
                <Citation key={id} id={id} trail={lead.supporting_evidence_ids} />
              ))}
            </div>
          )}
        </div>
      )}
      <p className="text-sm text-muted-foreground" data-testid="no-root-cause-hint">
        {failed
          ? `Some agents failed (${failed}), so the evidence is incomplete. Check them, then re-run the investigation.`
          : "The evidence is too weak or inconsistent. Re-run the investigation, or widen the time window."}
      </p>
    </div>
  );
}

/** One event; `context` rows (related tickets) are older than the incident: muted, with a date. */
function TimelineRow({
  t,
  trail,
  context = false,
}: {
  t: TimelineEvent;
  trail: string[];
  context?: boolean;
}) {
  const { open, highlighted, setHighlighted } = useEvidence();
  const active = t.evidence_id && highlighted === t.evidence_id;
  return (
    <motion.li variants={itemVariants} className="relative">
      <span
        aria-hidden
        className={cn(
          "absolute top-3 -left-[25px] size-2.5 rounded-full border-2 border-card",
          active
            ? "bg-primary"
            : context || t.source === "aiops"
              ? "bg-muted-foreground"
              : "bg-danger/80",
        )}
      />
      <button
        type="button"
        disabled={!t.evidence_id}
        onClick={() => t.evidence_id && open(t.evidence_id, trail)}
        onMouseEnter={() => t.evidence_id && setHighlighted(t.evidence_id)}
        className={cn(
          "grid w-full gap-3 rounded-md px-2 py-1.5 text-left text-sm transition-colors enabled:cursor-pointer enabled:hover:bg-accent",
          context ? "grid-cols-[6.5rem_1fr]" : "grid-cols-[4.5rem_1fr]",
          active && "bg-accent",
        )}
      >
        <time
          dateTime={t.timestamp}
          className="font-mono text-xs text-muted-foreground tabular-nums"
        >
          {context ? formatDateTime(t.timestamp).replace(" UTC", "") : formatClock(t.timestamp)}
        </time>
        <span>
          {t.description} <span className="text-xs text-muted-foreground">· {t.source}</span>
        </span>
      </button>
    </motion.li>
  );
}

function Timeline({ inv }: { inv: Investigation }) {
  const { evidence } = useEvidence();
  const [showRelated, setShowRelated] = useState(false);
  const kindOf = useCallback((id: string) => evidence.get(id)?.kind, [evidence]);
  const { chain, related } = useMemo(
    () => splitTimeline(inv.timeline, kindOf),
    [inv.timeline, kindOf],
  );
  const trail = useMemo(
    () => [...chain, ...related].flatMap((t) => (t.evidence_id ? [t.evidence_id] : [])),
    [chain, related],
  );
  if (inv.timeline.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Why it happened</CardTitle>
        <CardDescription>
          The chain of events (UTC) · click an event to see its evidence
        </CardDescription>
      </CardHeader>
      <CardContent>
        <motion.ol
          className="relative space-y-1 border-l pl-5"
          variants={listVariants}
          initial="hidden"
          animate="show"
          data-testid="timeline"
        >
          {chain.map((t, i) => (
            <TimelineRow key={`${t.timestamp}-${i}`} t={t} trail={trail} />
          ))}
        </motion.ol>
        {related.length > 0 && (
          <div className="mt-2 border-t pt-2" data-testid="timeline-related">
            <button
              type="button"
              aria-expanded={showRelated}
              aria-controls="timeline-related-list"
              onClick={() => setShowRelated((v) => !v)}
              className="flex w-full cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm text-muted-foreground transition-colors hover:bg-accent"
            >
              <ChevronRightIcon
                aria-hidden
                className={cn("size-4 transition-transform", showRelated && "rotate-90")}
              />
              <TicketIcon aria-hidden className="size-4" />
              {relatedLabel(related, kindOf)}
              <span className="text-xs">· context, not part of the chain</span>
            </button>
            {showRelated && (
              <motion.ol
                id="timeline-related-list"
                className="relative mt-1 space-y-1 border-l pl-5"
                variants={listVariants}
                initial="hidden"
                animate="show"
              >
                {related.map((t, i) => (
                  <TimelineRow key={`${t.timestamp}-r${i}`} t={t} trail={trail} context />
                ))}
              </motion.ol>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function SummaryTab({ inv }: { inv: Investigation }) {
  const r = inv.report!;
  // PR-033: prefer the RCA agent's typed claims; fall back to each agent's findings
  const findings: (Investigation["claims"][number] & { agent: string })[] = inv.claims.length
    ? inv.claims.map((c) => ({ ...c, agent: "rca" }))
    : inv.results.flatMap((res) => res.findings.map((f) => ({ ...f, agent: res.agent })));
  return (
    <div className="grid gap-5 lg:grid-cols-5">
      <div className="space-y-5 lg:col-span-3">
        <Card>
          <CardHeader>
            <CardTitle>Summary</CardTitle>
          </CardHeader>
          <CardContent className="text-sm leading-relaxed">{r.summary}</CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Key findings</CardTitle>
            <CardDescription>Every claim is typed and cites its evidence</CardDescription>
          </CardHeader>
          <CardContent>
            {findings.length === 0 ? (
              <p className="text-sm text-muted-foreground">No findings.</p>
            ) : (
              <ul className="space-y-3">
                {findings.map((f) => (
                  <li key={f.id} className="space-y-1.5" data-testid="finding">
                    <div className="flex flex-wrap items-center gap-2">
                      <ClaimBadge kind={f.kind} />
                      <span className="text-xs text-muted-foreground">
                        {agentMeta(f.agent).label}
                      </span>
                      {f.confidence ? (
                        <span className="text-xs text-muted-foreground tabular-nums">
                          {formatPercent(f.confidence)}
                        </span>
                      ) : null}
                    </div>
                    <p className="text-sm">{f.description}</p>
                    <div className="flex flex-wrap gap-1">
                      {f.evidence_ids.map((id) => (
                        <Citation key={id} id={id} trail={f.evidence_ids} />
                      ))}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
      <div className="space-y-5 lg:col-span-2">
        <Card>
          <CardHeader>
            <CardTitle>Hypotheses</CardTitle>
            <CardDescription>Ranked by the RCA agent, with contradicting evidence</CardDescription>
          </CardHeader>
          <CardContent>
            {inv.hypotheses.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                {reportOutcome(inv)?.kind === "healthy"
                  ? "No hypothesis: the evidence shows a healthy baseline."
                  : "No hypothesis: the evidence is too weak to rank one."}
              </p>
            ) : (
              <ol className="space-y-4">
                {inv.hypotheses.map((h, i) => (
                  <li key={h.id} className="space-y-1.5">
                    <div className="flex items-start justify-between gap-3">
                      <p className={cn("text-sm", i === 0 && "font-medium")}>
                        <ClaimBadge kind="HYPOTHESIS" /> {h.statement}
                      </p>
                      <span className="shrink-0 text-sm tabular-nums">
                        {formatPercent(h.confidence)}
                      </span>
                    </div>
                    {h.supporting_evidence_ids.length > 0 && (
                      <div className="flex flex-wrap items-center gap-1">
                        <span className="text-xs text-muted-foreground">supports:</span>
                        {h.supporting_evidence_ids.map((id) => (
                          <Citation key={id} id={id} trail={h.supporting_evidence_ids} />
                        ))}
                      </div>
                    )}
                    {h.contradicting_evidence_ids.length > 0 && (
                      <div
                        className="flex flex-wrap items-center gap-1"
                        data-testid="contradicting"
                      >
                        <span className="text-xs text-danger">contradicts:</span>
                        {h.contradicting_evidence_ids.map((id) => (
                          <Citation
                            key={id}
                            id={id}
                            tone="contra"
                            trail={h.contradicting_evidence_ids}
                          />
                        ))}
                      </div>
                    )}
                  </li>
                ))}
              </ol>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Agents</CardTitle>
            <CardDescription>
              {inv.results.length} runs · {formatDuration(inv.duration_ms)} ·{" "}
              {inv.usage.input_tokens + inv.usage.output_tokens} tokens · {inv.mode}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="space-y-3">
              {inv.results.map((res) => {
                const meta = agentMeta(res.agent);
                const status = res.status === "failed" ? "failed" : "done";
                return (
                  <li key={res.task_id} className="flex gap-3">
                    <meta.icon
                      aria-hidden
                      className="mt-0.5 size-4 shrink-0 text-muted-foreground"
                    />
                    <div className="min-w-0 flex-1 space-y-1">
                      <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
                        {meta.label}
                        <StepBadge status={status} />
                        <span className="text-xs font-normal text-muted-foreground">
                          {formatDuration(res.duration_ms)}
                        </span>
                      </p>
                      <p className="text-xs text-muted-foreground">{res.summary}</p>
                      {res.signals.length > 0 && (
                        <p className="flex flex-wrap gap-1">
                          {res.signals.map((s) => (
                            <span key={s} className="rounded-full border px-1.5 text-[10.5px]">
                              {humanizeSignal(s)}
                            </span>
                          ))}
                        </p>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          </CardContent>
        </Card>
        <CostCard inv={inv} />
      </div>
    </div>
  );
}

function EvidenceTab({ items, empty, icon }: { items: Ev[]; empty: string; icon: LucideIcon }) {
  const { open, highlighted } = useEvidence();
  const trail = useMemo(() => items.map((e) => e.id), [items]);
  if (items.length === 0)
    return (
      <EmptyState icon={icon} title={empty}>
        The agents found nothing of this kind for this incident. The other tabs and the timeline
        show what they did find.
      </EmptyState>
    );
  return (
    <motion.ul className="grid gap-4" variants={listVariants} initial="hidden" animate="show">
      {items.map((e) => (
        <motion.li key={e.id} variants={itemVariants}>
          <Card className={cn("transition-shadow", highlighted === e.id && "ring-2 ring-ring")}>
            <CardContent className="space-y-3 pt-(--pad)">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0 space-y-0.5">
                  <p className="text-sm font-medium">{e.summary}</p>
                  <p className="text-xs text-muted-foreground">
                    {agentMeta(e.agent).label} · {e.source}
                    {e.timestamp && ` · ${formatClock(e.timestamp)} UTC`} ·{" "}
                    <button
                      type="button"
                      className="cursor-pointer font-mono hover:underline"
                      onClick={() => open(e.id, trail)}
                    >
                      {e.id}
                    </button>
                  </p>
                </div>
                <DeepLink e={e} />
              </div>
              <EvidenceBody e={e} />
            </CardContent>
          </Card>
        </motion.li>
      ))}
    </motion.ul>
  );
}

function NextStepsTab({ inv }: { inv: Investigation }) {
  const r = inv.report!;
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Next steps</CardTitle>
        </CardHeader>
        <CardContent>
          <ol className="list-decimal space-y-2 pl-5 text-sm">
            {r.next_steps.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ol>
          {r.open_questions.length > 0 && (
            <>
              <p className="mt-5 mb-2 text-xs font-medium text-muted-foreground">Open questions</p>
              <ul className="list-disc space-y-1 pl-5 text-sm">
                {r.open_questions.map((q) => (
                  <li key={q}>{q}</li>
                ))}
              </ul>
            </>
          )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Recommendations</CardTitle>
          <CardDescription>Write actions run only after an approval</CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="space-y-4">
            {inv.recommendations.map((rec) => (
              <li key={rec.id} className="space-y-1.5">
                <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
                  <ClaimBadge kind="RECOMMENDATION" />
                  {rec.action}
                </p>
                <p className="text-xs text-muted-foreground">{rec.rationale}</p>
                <p className="flex flex-wrap items-center gap-1.5">
                  <Badge
                    tone={
                      rec.risk === "high" ? "danger" : rec.risk === "medium" ? "warn" : "neutral"
                    }
                  >
                    risk {rec.risk}
                  </Badge>
                  {rec.requires_approval && <Badge tone="purple">needs approval</Badge>}
                  {rec.evidence_ids.map((id) => (
                    <Citation key={id} id={id} trail={rec.evidence_ids} />
                  ))}
                </p>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}

const TABS: {
  value: string;
  label: string;
  icon: LucideIcon;
  kind?: EvidenceKind;
  empty?: string;
}[] = [
  { value: "summary", label: "Summary", icon: FileTextIcon },
  { value: "logs", label: "Logs", icon: ScrollTextIcon, kind: "log", empty: "No log evidence" },
  {
    value: "metrics",
    label: "Metrics",
    icon: ChartLineIcon,
    kind: "metric",
    empty: "No metric evidence",
  },
  { value: "alerts", label: "Alerts", icon: BellRingIcon, kind: "alert", empty: "No alerts" },
  {
    value: "k8s",
    label: "Kubernetes",
    icon: BoxesIcon,
    kind: "k8s_event",
    empty: "No Kubernetes evidence",
  },
  {
    value: "changes",
    label: "Changes",
    icon: GitCommitHorizontalIcon,
    kind: "commit",
    empty: "No recent changes",
  },
  {
    value: "tickets",
    label: "Tickets",
    icon: TicketIcon,
    kind: "ticket",
    empty: "No related tickets",
  },
  {
    value: "runbooks",
    label: "Runbooks",
    icon: BookOpenIcon,
    kind: "doc",
    empty: "No matching runbooks",
  },
  { value: "next", label: "Next steps", icon: ListChecksIcon },
];

/** Report mode (PR-038): hero, timeline, tabs, clickable evidence citations and actions. */
export function ReportView({ inv }: { inv: Investigation }) {
  const evidence = useMemo(() => collectEvidence(inv), [inv]);
  const byKind = useMemo(() => {
    const m = new Map<EvidenceKind, Ev[]>();
    for (const e of evidence.values()) m.set(e.kind, [...(m.get(e.kind) ?? []), e]);
    return m;
  }, [evidence]);
  if (!inv.report) {
    return (
      <EmptyState
        icon={inv.status === "failed" ? TriangleAlertIcon : FileQuestionIcon}
        title={inv.status === "failed" ? "The investigation failed before a report" : "No report"}
      >
        {inv.status === "cancelled" ? "It was cancelled." : "Re-run it to try again."}
      </EmptyState>
    );
  }
  const kind = (k: EvidenceKind) => byKind.get(k) ?? NO_EVIDENCE;
  // Hierarchy: the root cause first, then the "why" (timeline), then the evidence tabs.
  return (
    <EvidenceProvider evidence={evidence}>
      <div className="space-y-6" data-testid="report">
        <RootCauseHero inv={inv} />
        <ReportActions inv={inv} />
        <Timeline inv={inv} />
        <Tabs defaultValue="summary">
          <TabsList
            aria-label="Report sections"
            data-testid="report-tabs"
            className="sticky top-16 z-20 w-full justify-start glass-chrome shadow-elev-2"
          >
            {TABS.map((t) => {
              const n = t.kind ? kind(t.kind).length : null;
              return (
                <TabsTrigger key={t.value} value={t.value}>
                  <t.icon aria-hidden />
                  {t.label}
                  {n !== null && n > 0 && (
                    <span className="text-xs text-muted-foreground tabular-nums">{n}</span>
                  )}
                </TabsTrigger>
              );
            })}
          </TabsList>
          <TabsContent value="summary">
            <SummaryTab inv={inv} />
          </TabsContent>
          {TABS.filter((t) => t.kind).map((t) => (
            <TabsContent key={t.value} value={t.value}>
              <EvidenceTab items={kind(t.kind!)} empty={t.empty!} icon={t.icon} />
            </TabsContent>
          ))}
          <TabsContent value="next">
            <NextStepsTab inv={inv} />
          </TabsContent>
        </Tabs>
      </div>
    </EvidenceProvider>
  );
}
