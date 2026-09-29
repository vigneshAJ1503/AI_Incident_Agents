"use client";

import { ExternalLinkIcon } from "lucide-react";
import dynamic from "next/dynamic";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { Skeleton } from "@/components/ui/skeleton";
import type { Evidence } from "@/lib/api/schemas";
import {
  AlertData,
  CommitData,
  DocData,
  K8sData,
  LogData,
  MetricData,
  TicketData,
  linkLabel,
  view,
} from "@/lib/evidence";
import { formatClock, formatDateTime, formatMetric } from "@/lib/format";
import { cn } from "@/lib/utils";

const MetricChart = dynamic(
  () => import("@/components/charts/metric-chart").then((m) => m.MetricChart),
  {
    ssr: false,
    loading: () => <Skeleton className="h-[200px] w-full" />,
  },
);

export function DeepLink({ e, size = "sm" }: { e: Evidence; size?: "sm" | "default" }) {
  if (!e.link) return null;
  return (
    <Button asChild variant="outline" size={size}>
      <a href={e.link} target="_blank" rel="noopener noreferrer">
        {linkLabel(e)} <ExternalLinkIcon />
        <span className="sr-only">(opens in a new tab)</span>
      </a>
    </Button>
  );
}

export function DiffHunk({ hunk }: { hunk: string }) {
  return (
    <div className="group/code relative">
      <CopyButton
        text={hunk}
        label="Copy code change"
        what="Code change copied"
        className="absolute top-1 right-1 z-10 bg-card/80 opacity-70 group-hover/code:opacity-100 focus-visible:opacity-100"
      />
      <pre
        className="overflow-x-auto rounded-md border bg-muted/40 py-2 pr-9 font-mono text-[12px] leading-5"
        aria-label="Diff"
      >
        {hunk.split("\n").map((line, i) => (
          <div
            key={i}
            className={cn(
              "px-3 whitespace-pre",
              line.startsWith("+") && "bg-ok-bg text-ok",
              line.startsWith("-") && "bg-danger-bg text-danger",
              line.startsWith("@@") && "text-info",
            )}
          >
            <span aria-hidden className="mr-2 inline-block w-3 opacity-60 select-none">
              {line.startsWith("+") ? "+" : line.startsWith("-") ? "−" : " "}
            </span>
            {line.startsWith("+") || line.startsWith("-") ? line.slice(1) : line}
          </div>
        ))}
      </pre>
    </div>
  );
}

/** A monospace block (query, log samples) with a copy button. */
export function CodeBlock({ code, label, what }: { code: string; label: string; what: string }) {
  return (
    <div className="group/code relative">
      <CopyButton
        text={code}
        label={label}
        what={what}
        className="absolute top-1 right-1 z-10 bg-card/80 opacity-70 group-hover/code:opacity-100 focus-visible:opacity-100"
      />
      <pre className="overflow-x-auto rounded-md bg-muted/50 p-2.5 pr-10 font-mono text-[11.5px] leading-5 whitespace-pre-wrap">
        {code}
      </pre>
    </div>
  );
}

/** Kind-specific rendering of one evidence item (used in tabs and in the drawer). */
export function EvidenceBody({ e, compact = false }: { e: Evidence; compact?: boolean }) {
  switch (e.kind) {
    case "log": {
      const d = view(LogData, e);
      if (!d) break;
      return (
        <div className="space-y-2">
          <p className="font-mono text-[12.5px] break-words">{d.pattern}</p>
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground tabular-nums">
            {d.level && (
              <Badge
                tone={d.level === "ERROR" ? "danger" : d.level === "WARN" ? "warn" : "neutral"}
              >
                {d.level}
              </Badge>
            )}
            {d.count !== undefined && (
              <span>
                <strong className="text-foreground">{d.count}</strong> events
                {d.baseline_count !== undefined && ` · baseline ${d.baseline_count}`}
              </span>
            )}
            {d.first_seen && <span>first {formatClock(d.first_seen)}</span>}
            {d.last_seen && <span>last {formatClock(d.last_seen)}</span>}
          </div>
          {!compact && d.samples.length > 0 && (
            <CodeBlock
              code={d.samples.join("\n")}
              label="Copy log samples"
              what="Log samples copied"
            />
          )}
        </div>
      );
    }
    case "metric": {
      const d = view(MetricData, e);
      if (!d) break;
      return (
        <div className="space-y-2">
          {d.baseline !== undefined && d.peak !== undefined && (
            <p className="text-xs text-muted-foreground tabular-nums">
              baseline{" "}
              <strong className="text-foreground">{formatMetric(d.baseline, d.unit)}</strong> → peak{" "}
              <strong className="text-foreground">{formatMetric(d.peak, d.unit)}</strong>
              {d.anomaly && ` · anomaly from ${formatClock(d.anomaly.start)}`}
            </p>
          )}
          <MetricChart data={d} height={compact ? 160 : 200} />
        </div>
      );
    }
    case "commit": {
      const d = view(CommitData, e);
      if (!d) break;
      return (
        <div className="space-y-2">
          <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-foreground">
              {d.short_sha ?? d.sha.slice(0, 10)}
            </code>
            {d.subject && <span className="font-medium text-foreground">{d.subject}</span>}
            {d.author && <span>by {d.author}</span>}
            {d.tag && <Badge tone="info">{d.tag}</Badge>}
            {d.risky && <Badge tone="danger">risky change</Badge>}
          </p>
          {d.files.map((f) => (
            <div key={f.path} className="space-y-1">
              <p className="font-mono text-[11.5px] text-muted-foreground">
                {f.path} {f.status && <span>({f.status})</span>}
              </p>
              {f.hunk && <DiffHunk hunk={f.hunk} />}
            </div>
          ))}
        </div>
      );
    }
    case "alert": {
      const d = view(AlertData, e);
      if (!d || !d.alertname) break;
      return (
        <p className="flex flex-wrap items-center gap-2 text-xs">
          <span className="font-mono font-medium">{d.alertname}</span>
          {d.severity && (
            <Badge tone={d.severity === "critical" ? "danger" : "warn"}>{d.severity}</Badge>
          )}
          {d.state && <Badge tone="neutral">{d.state}</Badge>}
          {d.starts_at && (
            <span className="text-muted-foreground">since {formatDateTime(d.starts_at)}</span>
          )}
        </p>
      );
    }
    case "ticket": {
      const d = view(TicketData, e);
      if (!d) break;
      return (
        <p className="flex flex-wrap items-center gap-2 text-xs">
          <span className="font-mono font-medium">{d.key}</span>
          {d.status && <Badge tone={d.status === "Done" ? "ok" : "warn"}>{d.status}</Badge>}
          {d.priority && <Badge tone="neutral">{d.priority}</Badge>}
          {d.summary && <span className="text-muted-foreground">{d.summary}</span>}
        </p>
      );
    }
    case "doc": {
      const d = view(DocData, e);
      if (!d) break;
      return (
        <div className="space-y-1.5">
          <p className="text-xs text-muted-foreground">
            <span className="font-medium text-foreground">{d.title ?? d.path}</span>
            {d.section && ` · § ${d.section}`} · <span className="font-mono">{d.path}</span>
          </p>
          {d.excerpt && !compact && (
            <blockquote className="border-l-2 border-primary/40 pl-3 text-sm whitespace-pre-line text-muted-foreground">
              {d.excerpt}
            </blockquote>
          )}
        </div>
      );
    }
    case "k8s_event": {
      const d = view(K8sData, e);
      if (!d) break;
      return (
        <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          {d.reason && (
            <Badge tone={/OOM|BackOff|Fail/i.test(d.reason) ? "danger" : "neutral"}>
              {d.reason}
            </Badge>
          )}
          {d.object && <span className="font-mono">{d.object}</span>}
          {d.revision && <span>revision {d.revision}</span>}
          {d.change_cause && <span>“{d.change_cause}”</span>}
        </p>
      );
    }
  }
  return null;
}
