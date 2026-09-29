/**
 * Loose, optional views over Evidence.data per kind. The contract keeps `data` free-form, so the
 * UI renders these when present and falls back to the summary otherwise.
 */
import { z } from "zod";

import type { Evidence, Investigation } from "@/lib/api/schemas";

const num = z.number();
const str = z.string();

export const LogData = z.object({
  pattern: str,
  level: str.optional(),
  count: num.optional(),
  baseline_count: num.optional(),
  first_seen: str.optional(),
  last_seen: str.optional(),
  samples: z.array(str).default([]),
});

export const MetricData = z.object({
  metric: str.optional(),
  unit: str.default(""),
  baseline: num.optional(),
  peak: num.optional(),
  anomaly: z.object({ start: str, end: str }).nullable().optional(),
  series: z.array(z.object({ label: str, points: z.array(z.object({ t: str, v: num })) })).min(1),
});
export type MetricData = z.infer<typeof MetricData>;

export const CommitData = z.object({
  sha: str,
  short_sha: str.optional(),
  author: str.optional(),
  subject: str.optional(),
  tag: str.optional(),
  risky: z.boolean().optional(),
  files: z.array(z.object({ path: str, status: str.optional(), hunk: str.optional() })).default([]),
});

export const AlertData = z.object({
  alertname: str.nullable().optional(),
  severity: str.optional(),
  state: str.optional(),
  starts_at: str.optional(),
  runbook_url: str.optional(),
});

export const TicketData = z.object({
  key: str,
  status: str.optional(),
  priority: str.optional(),
  summary: str.optional(),
});

export const DocData = z.object({
  path: str,
  title: str.optional(),
  section: str.optional(),
  excerpt: str.optional(),
  rank: num.optional(),
});

export const K8sData = z.object({
  reason: str.optional(),
  object: str.optional(),
  change_cause: str.optional(),
  revision: str.optional(),
});

export function view<S extends z.ZodType>(schema: S, e: Evidence): z.infer<S> | null {
  const r = schema.safeParse(e.data);
  return r.success ? r.data : null;
}

/** All evidence of an investigation, de-duplicated by id, with the agent that found it. */
export function collectEvidence(inv: Investigation): Map<string, Evidence & { agent: string }> {
  const out = new Map<string, Evidence & { agent: string }>();
  for (const r of inv.results)
    for (const e of r.evidence) if (!out.has(e.id)) out.set(e.id, { ...e, agent: r.agent });
  return out;
}

/** The next id in a citation trail (wraps around); the first one if `current` isn't in it. */
export function stepTrail(trail: readonly string[], current: string, delta: 1 | -1): string | null {
  if (trail.length === 0) return null;
  const i = trail.indexOf(current);
  if (i === -1) return trail[0]!;
  return trail[(i + delta + trail.length) % trail.length]!;
}

export function linkLabel(e: Evidence): string {
  const link = e.link ?? "";
  if (link.includes(":5601")) return "View in Kibana";
  if (link.includes(":3000")) return "Open in Grafana";
  if (link.includes(":9093")) return "Open in Alertmanager";
  if (link.includes("/browse/")) return "Open ticket";
  if (link.includes("knowledge-base")) return "Open runbook";
  return "Open source";
}
