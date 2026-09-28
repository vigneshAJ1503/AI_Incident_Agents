import type { Route } from "next";
import Link from "next/link";

import { ConfidenceMeter, SeverityBadge, StatusBadge } from "@/components/status";
import type { InvestigationSummary } from "@/lib/api/schemas";
import { formatDuration, formatRelative } from "@/lib/format";

/** Compact list item for the dashboard's "Recent investigations". */
export function InvestigationListItem({ inv }: { inv: InvestigationSummary }) {
  return (
    <Link
      href={`/investigations/${inv.id}` as Route}
      className="group flex flex-col gap-2 rounded-lg px-3 py-3 transition-colors hover:bg-accent sm:flex-row sm:items-center sm:gap-4"
    >
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium group-hover:text-accent-foreground">
          {inv.incident.title}
        </p>
        <p className="truncate text-xs text-muted-foreground">
          {inv.incident.service ?? "service unknown"} · {inv.incident.id} ·{" "}
          {formatRelative(inv.created_at)}
          {inv.duration_ms ? ` · ${formatDuration(inv.duration_ms)}` : ""}
        </p>
      </div>
      <div className="flex items-center gap-3">
        {inv.report && inv.report.severity !== "none" && (
          <SeverityBadge severity={inv.report.severity} />
        )}
        <ConfidenceMeter value={inv.report?.confidence} />
        <StatusBadge status={inv.status} />
      </div>
    </Link>
  );
}
