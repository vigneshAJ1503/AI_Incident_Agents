"use client";

import { useQueryClient } from "@tanstack/react-query";
import { FlaskConicalIcon, PlayIcon, SyringeIcon, Undo2Icon } from "lucide-react";
import type { Route } from "next";
import { useRouter } from "next/navigation";
import type * as React from "react";
import { toast } from "sonner";

import { Stagger, StaggerItem } from "@/components/motion";
import { PageHeader } from "@/components/page-header";
import { ErrorState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip } from "@/components/ui/tooltip";
import { getClient } from "@/lib/api";
import { DEMO_MODE } from "@/lib/config";
import { useCreateInvestigation, useHealth, useScenarios } from "@/lib/queries";

/** The question each scenario's ground truth expects (scenarios/S*∕expected.yaml). */
const QUESTIONS: Record<string, string> = {
  S1: "Payment API is returning HTTP 500 in production",
  S2: "Orders are failing intermittently in production",
  S3: "Why are orders timing out in production?",
  S4: "Login and checkout requests are failing in production",
  S5: "Payments are slow in production",
};

export default function ScenariosPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const { data, isLoading, error, refetch } = useScenarios();
  const health = useHealth();
  const create = useCreateInvestigation();
  const faults = health.data?.faults_enabled ?? false;
  const disabledWhy = DEMO_MODE
    ? "Fault injection needs the backend and cluster; demo mode replays recordings instead."
    : "Disabled by the backend: set AIOPS_ENABLE_FAULTS=1 to allow live fault injection.";

  const act = async (fn: () => Promise<void>, ok: string) => {
    try {
      await fn();
      toast.success(ok);
      void qc.invalidateQueries({ queryKey: ["scenarios"] });
    } catch (err) {
      toast.error("Refused", { description: (err as Error).message });
    }
  };

  const runDemo = async (id: string, service: string) => {
    try {
      const res = await create.mutateAsync({
        question: QUESTIONS[id] ?? `Investigate ${id}`,
        service,
      });
      router.push(`/investigations/${res.id}` as Route);
    } catch (err) {
      toast.error("Could not start", { description: (err as Error).message });
    }
  };

  const guard = (node: React.ReactElement) =>
    faults ? (
      node
    ) : (
      <Tooltip content={disabledWhy}>
        <span tabIndex={0}>{node}</span>
      </Tooltip>
    );

  return (
    <div>
      <PageHeader
        title="Scenarios"
        description="Dev/demo incident scenarios (scenarios/S1–S5). Inject a fault into the local cluster, or replay a recorded investigation."
        actions={guard(
          <Button
            variant="outline"
            disabled={!faults}
            onClick={() =>
              void act(() => getClient().revertScenarios(), "Reverted to the healthy baseline")
            }
          >
            <Undo2Icon /> Revert all
          </Button>,
        )}
      />
      {isLoading && (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3" aria-busy="true">
          {Array.from({ length: 5 }, (_, i) => (
            <Skeleton key={i} className="h-48 rounded-xl" />
          ))}
        </div>
      )}
      {error && <ErrorState error={error} onRetry={() => void refetch()} />}
      {data && (
        <Stagger className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {data.map((s) => (
            <StaggerItem key={s.id}>
              <Card className="flex h-full flex-col" data-testid={`scenario-${s.id}`}>
                <CardHeader>
                  <div className="flex items-center gap-2">
                    <Badge tone="info" className="font-mono">
                      {s.id}
                    </Badge>
                    {s.active && <Badge tone="danger">active</Badge>}
                    <span className="ml-auto font-mono text-xs text-muted-foreground">
                      {s.service}
                    </span>
                  </div>
                  <CardTitle className="leading-snug">{s.title}</CardTitle>
                  <CardDescription>{s.description}</CardDescription>
                </CardHeader>
                <CardContent className="mt-auto flex flex-wrap gap-2">
                  {guard(
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={!faults}
                      onClick={() =>
                        void act(() => getClient().injectScenario(s.id), `${s.id} injected`)
                      }
                    >
                      <SyringeIcon /> Inject
                    </Button>,
                  )}
                  {DEMO_MODE && (
                    <Button
                      size="sm"
                      onClick={() => void runDemo(s.id, s.service)}
                      disabled={create.isPending}
                    >
                      <PlayIcon /> Run demo investigation
                    </Button>
                  )}
                </CardContent>
              </Card>
            </StaggerItem>
          ))}
        </Stagger>
      )}
      {!DEMO_MODE && !faults && (
        <p className="mt-6 flex items-center gap-2 text-xs text-muted-foreground">
          <FlaskConicalIcon aria-hidden className="size-3.5" /> {disabledWhy}
        </p>
      )}
    </div>
  );
}
