"use client";

import { BookOpenIcon, NetworkIcon, UsersIcon } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";

import { Stagger, StaggerItem } from "@/components/motion";
import { PageHeader } from "@/components/page-header";
import { ErrorState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useServices } from "@/lib/queries";

const RUNBOOKS =
  "https://github.com/vigneshAJ1503/AI_Incident_Agents/blob/main/knowledge-base/runbooks/";

export default function ServicesPage() {
  const { data, isLoading, error, refetch } = useServices();
  return (
    <div>
      <PageHeader
        title="Services"
        description="The service catalog the agents resolve questions against (profiles/<name>/services.yaml)."
      />
      {isLoading && (
        <div className="grid gap-4 md:grid-cols-2" aria-busy="true">
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="h-52 rounded-xl" />
          ))}
        </div>
      )}
      {error && <ErrorState error={error} onRetry={() => void refetch()} />}
      {data && (
        <Stagger className="grid gap-4 md:grid-cols-2">
          {data.map((s) => (
            <StaggerItem key={s.name}>
              <Card className="h-full">
                <CardHeader>
                  <CardTitle className="font-mono">{s.name}</CardTitle>
                  <CardDescription>{s.description}</CardDescription>
                </CardHeader>
                <CardContent className="space-y-3 text-sm">
                  <p className="flex flex-wrap items-center gap-2">
                    <UsersIcon aria-hidden className="size-4 text-muted-foreground" />
                    {Object.entries(s.owners).map(([k, v]) => (
                      <Badge key={k} tone="neutral">
                        {k}: {v}
                      </Badge>
                    ))}
                  </p>
                  <p className="flex flex-wrap items-center gap-2">
                    <NetworkIcon aria-hidden className="size-4 text-muted-foreground" />
                    <span className="text-xs text-muted-foreground">depends on</span>
                    {s.depends_on.map((d) => (
                      <Badge key={d} tone="outline" className="font-mono">
                        {d}
                      </Badge>
                    ))}
                  </p>
                  <p className="flex flex-wrap items-center gap-2">
                    <BookOpenIcon aria-hidden className="size-4 text-muted-foreground" />
                    {s.runbooks.map((r) => (
                      <a
                        key={r}
                        className="font-mono text-xs text-primary hover:underline"
                        href={`${RUNBOOKS}${r}`}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {r}
                      </a>
                    ))}
                  </p>
                  <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    {s.environments.map((e) => (
                      <Badge key={e} tone="info">
                        {e}
                      </Badge>
                    ))}
                    <Link
                      className="ml-auto text-primary hover:underline"
                      href={"/investigations/new" as Route}
                    >
                      Investigate
                    </Link>
                  </p>
                </CardContent>
              </Card>
            </StaggerItem>
          ))}
        </Stagger>
      )}
    </div>
  );
}
