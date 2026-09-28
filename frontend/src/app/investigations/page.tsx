"use client";

import { FilterXIcon, SearchIcon } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { PageHeader } from "@/components/page-header";
import { EmptyState, ErrorState } from "@/components/states";
import {
  ConfidenceMeter,
  SEVERITY_META,
  STATUS_META,
  SeverityBadge,
  StatusBadge,
} from "@/components/status";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, NativeSelect } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import type { InvestigationStatus, Severity } from "@/lib/api/schemas";
import { formatDateTime, formatDuration, formatRelative } from "@/lib/format";
import { useInvestigations, useServices } from "@/lib/queries";
import { useDebounced } from "@/lib/use-debounced";
import { cn } from "@/lib/utils";

const PAGE = 20;

export default function InvestigationsPage() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<InvestigationStatus | "">("");
  const [service, setService] = useState("");
  const [severity, setSeverity] = useState<Severity | "">("");
  const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<(string | null)[]>([]);
  const dq = useDebounced(q);
  const services = useServices();
  const { data, isLoading, isFetching, error, refetch } = useInvestigations({
    q: dq,
    status,
    service,
    severity,
    limit: PAGE,
    cursor,
  });
  const filtered = Boolean(dq || status || service || severity);
  const reset = (fn: () => void) => {
    fn();
    setCursor(null);
    setHistory([]);
  };

  return (
    <div>
      <PageHeader
        title="Investigations"
        description="Every question asked, its status and the root cause the agents found."
      />
      <Card className="overflow-hidden">
        <div className="flex flex-col gap-2 border-b p-3 md:flex-row md:items-center" role="search">
          <div className="relative flex-1">
            <SearchIcon
              aria-hidden
              className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground"
            />
            <Input
              value={q}
              onChange={(e) => reset(() => setQ(e.target.value))}
              placeholder="Search questions, ids, summaries…"
              aria-label="Search investigations"
              className="pl-9"
            />
          </div>
          <div className="grid grid-cols-3 gap-2 md:flex">
            <NativeSelect
              aria-label="Filter by status"
              value={status}
              onChange={(e) => reset(() => setStatus(e.target.value as InvestigationStatus | ""))}
            >
              <option value="">All statuses</option>
              {Object.entries(STATUS_META).map(([k, m]) => (
                <option key={k} value={k}>
                  {m.label}
                </option>
              ))}
            </NativeSelect>
            <NativeSelect
              aria-label="Filter by service"
              value={service}
              onChange={(e) => reset(() => setService(e.target.value))}
            >
              <option value="">All services</option>
              {services.data?.map((s) => (
                <option key={s.name} value={s.name}>
                  {s.name}
                </option>
              ))}
            </NativeSelect>
            <NativeSelect
              aria-label="Filter by severity"
              value={severity}
              onChange={(e) => reset(() => setSeverity(e.target.value as Severity | ""))}
            >
              <option value="">All severities</option>
              {Object.entries(SEVERITY_META).map(([k, m]) => (
                <option key={k} value={k}>
                  {m.label}
                </option>
              ))}
            </NativeSelect>
          </div>
          {filtered && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() =>
                reset(() => {
                  setQ("");
                  setStatus("");
                  setService("");
                  setSeverity("");
                })
              }
            >
              <FilterXIcon /> Clear
            </Button>
          )}
        </div>

        {error ? (
          <ErrorState error={error} onRetry={() => void refetch()} className="m-4" />
        ) : (
          <div
            className={cn(
              "overflow-x-auto transition-opacity",
              isFetching && !isLoading && "opacity-70",
            )}
          >
            <table className="w-full min-w-[640px] text-sm" aria-busy={isLoading}>
              <caption className="sr-only">Investigations, newest first</caption>
              <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
                <tr>
                  <th scope="col" className="w-[42%] px-4 py-2.5 font-medium">
                    Incident
                  </th>
                  <th scope="col" className="px-3 py-2.5 font-medium">
                    Status
                  </th>
                  <th scope="col" className="hidden px-3 py-2.5 font-medium md:table-cell">
                    Severity
                  </th>
                  <th scope="col" className="px-3 py-2.5 font-medium">
                    Confidence
                  </th>
                  <th scope="col" className="hidden px-3 py-2.5 font-medium lg:table-cell">
                    Duration
                  </th>
                  <th scope="col" className="px-4 py-2.5 text-right font-medium">
                    Started
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {isLoading &&
                  Array.from({ length: 8 }, (_, i) => (
                    <tr key={i}>
                      <td className="px-4 py-3" colSpan={6}>
                        <Skeleton className="h-9 w-full" />
                      </td>
                    </tr>
                  ))}
                {data?.items.map((inv) => (
                  <tr
                    key={inv.id}
                    className="cursor-pointer transition-colors hover:bg-accent/60"
                    onClick={() => router.push(`/investigations/${inv.id}` as Route)}
                  >
                    <td className="max-w-0 px-4 py-3">
                      <Link
                        href={`/investigations/${inv.id}` as Route}
                        className="block truncate font-medium hover:underline"
                        onClick={(e) => e.stopPropagation()}
                      >
                        {inv.incident.title}
                      </Link>
                      <span className="block truncate text-xs text-muted-foreground">
                        {inv.incident.service ?? "service unknown"} ·{" "}
                        <span className="font-mono">{inv.id}</span>
                      </span>
                    </td>
                    <td className="px-3 py-3">
                      <StatusBadge status={inv.status} />
                    </td>
                    <td className="hidden px-3 py-3 md:table-cell">
                      {inv.report ? (
                        <SeverityBadge severity={inv.report.severity} />
                      ) : (
                        <span className="text-xs text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="px-3 py-3">
                      <ConfidenceMeter value={inv.report?.confidence} />
                    </td>
                    <td className="hidden px-3 py-3 text-xs tabular-nums lg:table-cell">
                      {formatDuration(inv.duration_ms)}
                    </td>
                    <td className="px-4 py-3 text-right text-xs whitespace-nowrap text-muted-foreground">
                      <time dateTime={inv.created_at} title={formatDateTime(inv.created_at)}>
                        {formatRelative(inv.created_at)}
                      </time>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data && data.items.length === 0 && (
              <EmptyState
                title={filtered ? "No investigations match these filters" : "No investigations yet"}
                className="m-4"
              >
                {filtered ? "Try clearing the filters." : "Ask a question to start the first one."}
              </EmptyState>
            )}
          </div>
        )}
        {data && (history.length > 0 || data.next_cursor) && (
          <div className="flex items-center justify-end gap-2 border-t p-3">
            <Button
              variant="outline"
              size="sm"
              disabled={history.length === 0}
              onClick={() => {
                setCursor(history.at(-1) ?? null);
                setHistory((h) => h.slice(0, -1));
              }}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={!data.next_cursor}
              onClick={() => {
                setHistory((h) => [...h, cursor]);
                setCursor(data.next_cursor ?? null);
              }}
            >
              Next
            </Button>
          </div>
        )}
      </Card>
    </div>
  );
}
