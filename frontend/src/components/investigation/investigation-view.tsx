"use client";

import { ArrowLeftIcon } from "lucide-react";
import Link from "next/link";

import { PageHeader } from "@/components/page-header";
import { ErrorState } from "@/components/states";
import { SeverityBadge, StatusBadge } from "@/components/status";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime, formatDuration, formatPercent } from "@/lib/format";
import { useInvestigation } from "@/lib/queries";

export function InvestigationView({ id }: { id: string }) {
  const { data: inv, isLoading, error, refetch } = useInvestigation(id);

  if (isLoading) {
    return (
      <div className="space-y-4" aria-busy="true">
        <Skeleton className="h-10 w-2/3" />
        <Skeleton className="h-40 w-full rounded-xl" />
      </div>
    );
  }
  if (error || !inv) return <ErrorState error={error} onRetry={() => void refetch()} />;

  return (
    <div className="space-y-6">
      <Button asChild variant="ghost" size="sm" className="-ml-2">
        <Link href="/investigations">
          <ArrowLeftIcon /> Investigations
        </Link>
      </Button>
      <PageHeader
        title={inv.incident.title}
        description={
          <>
            {inv.incident.service ?? "service unknown"} ·{" "}
            {inv.incident.environment ?? "environment unknown"} · {formatDateTime(inv.created_at)} ·{" "}
            {formatDuration(inv.duration_ms)}
          </>
        }
        actions={
          <>
            {inv.report && <SeverityBadge severity={inv.report.severity} />}
            <StatusBadge status={inv.status} />
          </>
        }
      />
      {inv.report && (
        <Card>
          <CardHeader>
            <CardTitle>Summary · confidence {formatPercent(inv.report.confidence)}</CardTitle>
          </CardHeader>
          <CardContent className="text-sm leading-relaxed">{inv.report.summary}</CardContent>
        </Card>
      )}
    </div>
  );
}
