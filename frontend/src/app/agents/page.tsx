"use client";

import { agentMeta } from "@/components/investigation/agent-meta";
import { Stagger, StaggerItem } from "@/components/motion";
import { PageHeader } from "@/components/page-header";
import { ErrorState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDuration, formatPercent, formatRelative } from "@/lib/format";
import { useAgents } from "@/lib/queries";
import { cn } from "@/lib/utils";

export default function AgentsPage() {
  const { data, isLoading, error, refetch } = useAgents();
  return (
    <div>
      <PageHeader
        title="Agents"
        description="Specialist agents bind to capabilities, never vendors; the RCA agent correlates their findings."
      />
      {isLoading && (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-busy="true">
          {Array.from({ length: 8 }, (_, i) => (
            <Skeleton key={i} className="h-48 rounded-xl" />
          ))}
        </div>
      )}
      {error && <ErrorState error={error} onRetry={() => void refetch()} />}
      {data && (
        <Stagger className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {data.map((a) => {
            const meta = agentMeta(a.name);
            const rate = a.success_rate_7d ?? null;
            return (
              <StaggerItem key={a.name}>
                <Card className="h-full">
                  <CardContent className="flex h-full flex-col gap-3 pt-5">
                    <div className="flex items-center gap-3">
                      <span className="grid size-9 place-items-center rounded-lg bg-muted">
                        <meta.icon aria-hidden className="size-4.5" />
                      </span>
                      <div>
                        <h2 className="text-sm font-medium">{meta.label}</h2>
                        <p className="font-mono text-xs text-muted-foreground">
                          {a.name} · v{a.version}
                        </p>
                      </div>
                    </div>
                    <p className="flex-1 text-xs text-muted-foreground">{a.description}</p>
                    <div className="flex flex-wrap gap-1">
                      {a.capabilities.map((c) => (
                        <Badge key={c} tone="outline" className="font-mono">
                          {c}
                        </Badge>
                      ))}
                    </div>
                    <dl className="grid grid-cols-3 gap-2 border-t pt-3 text-xs">
                      <div>
                        <dt className="text-muted-foreground">Success 7d</dt>
                        <dd
                          className={cn(
                            "font-medium tabular-nums",
                            rate !== null && rate < 0.97 && "text-warn",
                          )}
                        >
                          {formatPercent(rate)}
                        </dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">p50</dt>
                        <dd className="font-medium tabular-nums">{formatDuration(a.p50_ms)}</dd>
                      </div>
                      <div>
                        <dt className="text-muted-foreground">Last run</dt>
                        <dd className="font-medium">{formatRelative(a.last_run_at)}</dd>
                      </div>
                    </dl>
                  </CardContent>
                </Card>
              </StaggerItem>
            );
          })}
        </Stagger>
      )}
    </div>
  );
}
