"use client";

import { useQueryClient } from "@tanstack/react-query";
import { FlaskConicalIcon, Loader2Icon, PlayIcon, SyringeIcon, Undo2Icon } from "lucide-react";
import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useState } from "react";
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
import type { FaultStatus } from "@/lib/api/schemas";
import { DEMO_MODE } from "@/lib/config";
import { POLL_MS, waitForInject, waitForRevert } from "@/lib/faults";
import { qk, useCreateInvestigation, useFaultStatus, useHealth, useScenarios } from "@/lib/queries";

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
  const health = useHealth();
  const faults = health.data?.faults_enabled ?? false;
  const status = useFaultStatus(faults);
  // what this page started and is waiting for; `status.reverting` also covers a reload mid-revert
  const [busy, setBusy] = useState<{ kind: "inject" | "revert"; id?: string } | null>(null);
  const reverting = busy?.kind === "revert" || (status.data?.reverting ?? false);
  const { data, isLoading, error, refetch } = useScenarios(busy || reverting ? POLL_MS : false);
  const create = useCreateInvestigation();
  const disabledWhy = DEMO_MODE
    ? "Fault injection needs the backend and cluster; demo mode replays recordings instead."
    : "Disabled by the backend: set AIOPS_ENABLE_FAULTS=1 to allow live fault injection.";

  const track = (s: FaultStatus) => qc.setQueryData(qk.faultStatus, s);
  const getStatus = () => getClient().scenarioStatus();

  // The API confirms the outcome: a revert runs in the background for minutes (202), so the toast
  // stays "Reverting…" until GET /scenarios/status says it finished (or why it failed).
  const revert = async () => {
    setBusy({ kind: "revert" });
    const t = toast.loading("Reverting to the healthy baseline…", {
      description: "Rolling the services back; this takes a few minutes.",
    });
    try {
      await getClient().revertScenarios();
      await waitForRevert(getStatus, { onStatus: track });
      toast.success("Reverted to the healthy baseline", { id: t, description: undefined });
    } catch (err) {
      toast.error("Revert failed", { id: t, description: (err as Error).message });
    } finally {
      setBusy(null);
      void qc.invalidateQueries({ queryKey: qk.scenarios });
    }
  };

  const inject = async (id: string) => {
    setBusy({ kind: "inject", id });
    const t = toast.loading(`Injecting ${id}…`);
    try {
      await getClient().injectScenario(id);
      await waitForInject(id, getStatus, { onStatus: track });
      toast.success(`${id} injected`, {
        id: t,
        description: "The incident develops over the next minutes.",
      });
    } catch (err) {
      toast.error(`Could not inject ${id}`, { id: t, description: (err as Error).message });
    } finally {
      setBusy(null);
      void qc.invalidateQueries({ queryKey: qk.scenarios });
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
            disabled={!faults || busy !== null || reverting}
            aria-busy={reverting}
            onClick={() => void revert()}
          >
            {reverting ? <Loader2Icon className="animate-spin" /> : <Undo2Icon />}
            {reverting ? "Reverting…" : "Revert all"}
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
                    {s.active && reverting ? (
                      <Badge tone="warn">reverting…</Badge>
                    ) : s.active ? (
                      <Badge tone="danger">active</Badge>
                    ) : busy?.kind === "inject" && busy.id === s.id ? (
                      <Badge tone="warn">injecting…</Badge>
                    ) : null}
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
                      disabled={!faults || busy !== null || reverting}
                      onClick={() => void inject(s.id)}
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
