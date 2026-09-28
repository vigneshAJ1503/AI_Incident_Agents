"use client";

import { ArrowLeftIcon, HistoryIcon } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { PageHeader } from "@/components/page-header";
import { ErrorState } from "@/components/states";
import { SeverityBadge, StatusBadge } from "@/components/status";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { Investigation } from "@/lib/api/schemas";
import { formatDateTime, formatDuration } from "@/lib/format";
import { isTerminal } from "@/lib/live/reducer";
import { useLiveInvestigation } from "@/lib/live/use-live";
import { useInvestigation } from "@/lib/queries";

import { LiveView } from "./live-view";
import { ReportView } from "./report-view";

const LIVE_STATUSES = new Set<Investigation["status"]>([
  "pending",
  "running",
  "needs_clarification",
]);

export function InvestigationView({ id }: { id: string }) {
  const { data: inv, isLoading, error, refetch } = useInvestigation(id);
  const [replay, setReplay] = useState(false);
  const liveFromApi = inv ? LIVE_STATUSES.has(inv.status) : false;
  const { state, stream, streamError } = useLiveInvestigation(id, liveFromApi || replay);
  // keep the live view until the refetched investigation is final (no flash of empty report)
  const showLive = replay || liveFromApi || (state.phase !== "idle" && !isTerminal(state.phase));

  if (isLoading) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading investigation">
        <Skeleton className="h-6 w-32" />
        <Skeleton className="h-10 w-2/3" />
        <Skeleton className="h-40 w-full rounded-xl" />
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="h-36 rounded-xl" />
          ))}
        </div>
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
            {inv.incident.environment ?? "environment unknown"} · {formatDateTime(inv.created_at)}
            {inv.duration_ms ? ` · ${formatDuration(inv.duration_ms)}` : ""} ·{" "}
            <span className="font-mono">{inv.id}</span>
          </>
        }
        actions={
          <>
            {inv.report && inv.report.severity !== "none" && (
              <SeverityBadge severity={inv.report.severity} />
            )}
            <StatusBadge
              status={
                showLive && !isTerminal(state.phase)
                  ? state.phase === "clarifying"
                    ? "needs_clarification"
                    : "running"
                  : inv.status
              }
            />
            {!showLive && inv.steps.length > 0 && (
              <Button variant="ghost" size="sm" onClick={() => setReplay(true)}>
                <HistoryIcon /> Replay run
              </Button>
            )}
          </>
        }
      />
      {showLive ? (
        <>
          <LiveView
            id={id}
            question={inv.incident.title}
            createdAt={inv.created_at}
            state={state}
            stream={stream}
            streamError={streamError}
          />
          {replay && isTerminal(state.phase) && (
            <div className="flex justify-center">
              <Button variant="outline" onClick={() => setReplay(false)}>
                Back to the report
              </Button>
            </div>
          )}
        </>
      ) : (
        <ReportView inv={inv} />
      )}
    </div>
  );
}
