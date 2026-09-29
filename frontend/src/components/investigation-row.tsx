"use client";

import { motion } from "framer-motion";
import { ChevronRightIcon } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { memo } from "react";

import { ConfidenceMeter, SeverityBadge, StatusBadge } from "@/components/status";
import type { InvestigationSummary } from "@/lib/api/schemas";
import { formatDuration, formatRelative } from "@/lib/format";

/**
 * Shared-layout id of an investigation's title: the row's title morphs into the detail page's
 * heading (Framer Motion `layoutId`; instant under prefers-reduced-motion).
 */
export const titleLayoutId = (id: string) => `inv-title-${id}`;

/** Compact list item for the dashboard's "Recent investigations". */
export const InvestigationListItem = memo(function InvestigationListItem({
  inv,
}: {
  inv: InvestigationSummary;
}) {
  return (
    <Link
      href={`/investigations/${inv.id}` as Route}
      className="group flex flex-col gap-2 rounded-lg px-3 py-(--row-y) transition-[background-color,box-shadow] hover:bg-accent/70 hover:shadow-[inset_0_0_0_1px_var(--glass-border)] sm:flex-row sm:items-center sm:gap-4"
    >
      <div className="min-w-0 flex-1">
        <motion.p
          layoutId={titleLayoutId(inv.id)}
          className="truncate text-sm font-medium group-hover:text-accent-foreground"
        >
          {inv.incident.title}
        </motion.p>
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
        <ChevronRightIcon
          aria-hidden
          className="hidden size-4 text-muted-foreground transition-transform group-hover:translate-x-0.5 sm:block"
        />
      </div>
    </Link>
  );
});
