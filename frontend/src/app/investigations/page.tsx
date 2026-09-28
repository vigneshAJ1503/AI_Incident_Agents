"use client";

import { motion } from "framer-motion";
import { FilterXIcon, SearchIcon, SearchXIcon, SparklesIcon } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { memo, useCallback, useState } from "react";

import { titleLayoutId } from "@/components/investigation-row";
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
import type { InvestigationStatus, InvestigationSummary, Severity } from "@/lib/api/schemas";
import { formatDateTime, formatDuration, formatRelative } from "@/lib/format";
import { useInvestigations, useServices } from "@/lib/queries";
import { useDebounced } from "@/lib/use-debounced";
import { cn } from "@/lib/utils";

const PAGE = 20;

/** One row; memoized so typing in the search box doesn't re-render every row. */
const Row = memo(function Row({
  inv,
  onOpen,
}: {
  inv: InvestigationSummary;
  onOpen: (id: string) => void;
}) {
  return (
    <tr
      className="cursor-pointer transition-colors hover:bg-accent/60"
      onClick={() => onOpen(inv.id)}
    >
      <td className="max-w-0 px-4 py-(--row-y)">
        <Link
          href={`/investigations/${inv.id}` as Route}
          className="block truncate font-medium hover:underline"
          onClick={(e) => e.stopPropagation()}
        >
          <motion.span layoutId={titleLayoutId(inv.id)} className="block truncate">
            {inv.incident.title}
          </motion.span>
        </Link>
        <span className="block truncate text-xs text-muted-foreground">
          {inv.incident.service ?? "service unknown"} · <span className="font-mono">{inv.id}</span>
        </span>
      </td>
      <td className="px-3 py-(--row-y)">
        <StatusBadge status={inv.status} />
      </td>
      <td className="hidden px-3 py-(--row-y) md:table-cell">
        {inv.report ? (
          <SeverityBadge severity={inv.report.severity} />
        ) : (
          <span className="text-xs text-muted-foreground">—</span>
        )}
      </td>
      <td className="px-3 py-(--row-y)">
        <ConfidenceMeter value={inv.report?.confidence} />
      </td>
      <td className="hidden px-3 py-(--row-y) text-xs tabular-nums lg:table-cell">
        {formatDuration(inv.duration_ms)}
      </td>
      <td className="px-4 py-(--row-y) text-right text-xs whitespace-nowrap text-muted-foreground">
        <time dateTime={inv.created_at} title={formatDateTime(inv.created_at)}>
          {formatRelative(inv.created_at)}
        </time>
      </td>
    </tr>
  );
});

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
  const open = useCallback((id: string) => router.push(`/investigations/${id}` as Route), [router]);
  const reset = (fn: () => void) => {
    fn();
    setCursor(null);
    setHistory([]);
  };
  const clearFilters = () =>
    reset(() => {
      setQ("");
      setStatus("");
      setService("");
      setSeverity("");
    });

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
            <Button variant="ghost" size="sm" onClick={clearFilters}>
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
              <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
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
                      <td className="px-4 py-(--row-y)" colSpan={6}>
                        <Skeleton className="h-9 w-full" />
                      </td>
                    </tr>
                  ))}
                {data?.items.map((inv) => (
                  <Row key={inv.id} inv={inv} onOpen={open} />
                ))}
              </tbody>
            </table>
            {data && data.items.length === 0 && (
              <EmptyState
                title={filtered ? "No investigations match these filters" : "No investigations yet"}
                icon={filtered ? SearchXIcon : SparklesIcon}
                className="m-4"
                action={
                  filtered ? (
                    <Button variant="outline" size="sm" onClick={clearFilters}>
                      <FilterXIcon /> Reset all filters
                    </Button>
                  ) : (
                    <Button asChild size="sm">
                      <Link href="/investigations/new">
                        <SparklesIcon /> New investigation
                      </Link>
                    </Button>
                  )
                }
              >
                {filtered
                  ? "Nothing matches the search and filters."
                  : "Ask a question to start the first one."}
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
