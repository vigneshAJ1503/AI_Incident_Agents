"use client";

import { EmptyState } from "@/components/states";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { Investigation } from "@/lib/api/schemas";
import { formatPercent } from "@/lib/format";

/** Report mode. PR-038 replaces this with the full report (hero, timeline, tabs, evidence drawer). */
export function ReportView({ inv }: { inv: Investigation }) {
  if (!inv.report) {
    return (
      <EmptyState
        title={inv.status === "failed" ? "The investigation failed before a report" : "No report"}
      >
        {inv.status === "cancelled" ? "It was cancelled." : "Re-run it to try again."}
      </EmptyState>
    );
  }
  return (
    <Card data-testid="report">
      <CardHeader>
        <CardTitle>Summary · confidence {formatPercent(inv.report.confidence)}</CardTitle>
      </CardHeader>
      <CardContent className="text-sm leading-relaxed">{inv.report.summary}</CardContent>
    </Card>
  );
}
