"use client";

import {
  ActivityIcon,
  ArrowRightIcon,
  BotIcon,
  CircleDotIcon,
  GaugeIcon,
  SparklesIcon,
  TargetIcon,
  TimerIcon,
  type LucideIcon,
} from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";

import { InvestigationListItem } from "@/components/investigation-row";
import { NumberTicker, Stagger, StaggerItem } from "@/components/motion";
import { PageHeader } from "@/components/page-header";
import { ErrorState } from "@/components/states";
import { SystemStatusStrip } from "@/components/system-status";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDuration, formatPercent, humanizeSignal } from "@/lib/format";
import { useDashboard } from "@/lib/queries";
import { cn } from "@/lib/utils";

const chartFallback = () => <Skeleton className="h-60 w-full" />;
const PerDayChart = dynamic(
  () => import("@/components/charts/dashboard-charts").then((m) => m.PerDayChart),
  {
    ssr: false,
    loading: chartFallback,
  },
);
const ByServiceChart = dynamic(
  () => import("@/components/charts/dashboard-charts").then((m) => m.ByServiceChart),
  {
    ssr: false,
    loading: chartFallback,
  },
);

function Kpi({
  icon: Icon,
  label,
  value,
  format,
  hint,
  accent,
}: {
  icon: LucideIcon;
  label: string;
  value: number;
  format?: (n: number) => string;
  hint?: string;
  accent: string;
}) {
  return (
    <Card className="h-full">
      <CardContent className="flex h-full flex-col gap-3 pt-5">
        <div className="flex items-center justify-between">
          <span className="text-xs font-medium text-muted-foreground">{label}</span>
          <span className={cn("grid size-7 place-items-center rounded-md", accent)}>
            <Icon aria-hidden className="size-4" />
          </span>
        </div>
        <NumberTicker
          value={value}
          format={format}
          className="text-2xl font-semibold tracking-tight tabular-nums"
        />
        {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
      </CardContent>
    </Card>
  );
}

function DashboardSkeleton() {
  return (
    <div className="space-y-6" aria-busy="true" aria-label="Loading dashboard">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        {Array.from({ length: 5 }, (_, i) => (
          <Skeleton key={i} className="h-[118px] rounded-xl" />
        ))}
      </div>
      <div className="grid gap-4 lg:grid-cols-3">
        <Skeleton className="h-80 rounded-xl lg:col-span-2" />
        <Skeleton className="h-80 rounded-xl" />
      </div>
    </div>
  );
}

export default function DashboardPage() {
  const { data, isLoading, error, refetch } = useDashboard(14);
  const maxSignal = Math.max(1, ...(data?.top_signals.map((s) => s.count) ?? [1]));

  return (
    <div className="space-y-6">
      <PageHeader
        title="Dashboard"
        description="Investigations, root causes and agent health over the last 14 days."
        actions={
          <>
            <Button asChild variant="outline">
              <Link href="/investigations">
                All investigations <ArrowRightIcon />
              </Link>
            </Button>
            <Button asChild>
              <Link href="/investigations/new">
                <SparklesIcon /> New investigation
              </Link>
            </Button>
          </>
        }
      />
      <SystemStatusStrip />
      {isLoading && <DashboardSkeleton />}
      {error && <ErrorState error={error} onRetry={() => void refetch()} />}
      {data && (
        <>
          <Stagger className="grid grid-cols-2 gap-4 lg:grid-cols-5">
            <StaggerItem>
              <Kpi
                icon={ActivityIcon}
                label="Investigations"
                value={data.totals.investigations}
                hint={`${data.window_days} days`}
                accent="bg-info-bg text-info"
              />
            </StaggerItem>
            <StaggerItem>
              <Kpi
                icon={TimerIcon}
                label="MTTR p50"
                value={data.mttr_minutes.p50}
                format={(n) => `${n.toFixed(1)} min`}
                hint={`p90 ${data.mttr_minutes.p90.toFixed(1)} min · question → report`}
                accent="bg-purple-bg text-purple"
              />
            </StaggerItem>
            <StaggerItem>
              <Kpi
                icon={TargetIcon}
                label="Root cause found"
                value={
                  data.totals.investigations
                    ? data.totals.root_cause_found / data.totals.investigations
                    : 0
                }
                format={(n) => formatPercent(n)}
                hint={`${data.totals.root_cause_found} found · ${data.totals.no_incident} no incident`}
                accent="bg-ok-bg text-ok"
              />
            </StaggerItem>
            <StaggerItem>
              <Kpi
                icon={GaugeIcon}
                label="Avg confidence"
                value={data.avg_confidence}
                format={(n) => formatPercent(n)}
                hint="of identified root causes"
                accent="bg-warn-bg text-warn"
              />
            </StaggerItem>
            <StaggerItem className="col-span-2 lg:col-span-1">
              <Kpi
                icon={CircleDotIcon}
                label="Open"
                value={data.totals.open}
                hint={`${data.totals.failed} failed in the window`}
                accent="bg-danger-bg text-danger"
              />
            </StaggerItem>
          </Stagger>

          <div className="grid gap-4 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader>
                <CardTitle>Investigations per day</CardTitle>
                <CardDescription>Stacked by severity of the identified root cause</CardDescription>
              </CardHeader>
              <CardContent>
                <PerDayChart data={data.by_day} />
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>By service</CardTitle>
                <CardDescription>Hover for the most frequent root cause</CardDescription>
              </CardHeader>
              <CardContent>
                <ByServiceChart data={data.by_service} />
              </CardContent>
            </Card>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>Top signals</CardTitle>
                <CardDescription>What the agents flagged most often</CardDescription>
              </CardHeader>
              <CardContent>
                <ul className="space-y-2.5">
                  {data.top_signals.map((s) => (
                    <li
                      key={s.signal}
                      className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1"
                    >
                      <span className="truncate text-sm">{humanizeSignal(s.signal)}</span>
                      <span className="text-xs text-muted-foreground tabular-nums">{s.count}</span>
                      <div className="col-span-2 h-1.5 overflow-hidden rounded-full bg-muted">
                        <div
                          className="h-full rounded-full bg-chart-1"
                          style={{ width: `${(s.count / maxSignal) * 100}%` }}
                        />
                      </div>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>Agent performance</CardTitle>
                <CardDescription>
                  Success rate and median latency per specialist agent
                </CardDescription>
              </CardHeader>
              <CardContent className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs text-muted-foreground">
                      <th className="pb-2 font-medium">Agent</th>
                      <th className="pb-2 text-right font-medium">Runs</th>
                      <th className="pb-2 pl-4 font-medium">Success</th>
                      <th className="pb-2 text-right font-medium">p50</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {data.agents.map((a) => (
                      <tr key={a.name}>
                        <td className="py-2">
                          <span className="flex items-center gap-2">
                            <BotIcon aria-hidden className="size-3.5 text-muted-foreground" />
                            {a.name}
                          </span>
                        </td>
                        <td className="py-2 text-right tabular-nums">{a.runs}</td>
                        <td className="py-2 pl-4">
                          <span className="flex items-center gap-2">
                            <span
                              className="h-1.5 w-16 overflow-hidden rounded-full bg-muted"
                              aria-hidden
                            >
                              <span
                                className={cn(
                                  "block h-full rounded-full",
                                  a.success_rate >= 0.97
                                    ? "bg-ok"
                                    : a.success_rate >= 0.9
                                      ? "bg-warn"
                                      : "bg-danger",
                                )}
                                style={{ width: `${a.success_rate * 100}%` }}
                              />
                            </span>
                            <span className="text-xs tabular-nums">
                              {formatPercent(a.success_rate)}
                            </span>
                          </span>
                        </td>
                        <td className="py-2 text-right text-xs tabular-nums">
                          {formatDuration(a.p50_ms)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader className="flex-row items-center justify-between">
              <div className="space-y-1">
                <CardTitle>Recent investigations</CardTitle>
                <CardDescription>Newest first</CardDescription>
              </div>
              <Button asChild variant="ghost" size="sm">
                <Link href="/investigations">
                  View all <ArrowRightIcon />
                </Link>
              </Button>
            </CardHeader>
            <CardContent className="px-2">
              <Stagger as="ul">
                {data.recent.map((inv) => (
                  <StaggerItem as="li" key={inv.id}>
                    <InvestigationListItem inv={inv} />
                  </StaggerItem>
                ))}
              </Stagger>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
