"use client";

import { ShieldCheckIcon } from "lucide-react";
import Link from "next/link";

import { ApprovalCard } from "@/components/approvals/approval-card";
import { Stagger, StaggerItem } from "@/components/motion";
import { PageHeader } from "@/components/page-header";
import { EmptyState, ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useApprovals } from "@/lib/queries";

export default function ApprovalsPage() {
  const { data, isLoading, error, refetch } = useApprovals();
  const pending = data?.filter((a) => a.status === "pending") ?? [];
  const decided = data?.filter((a) => a.status !== "pending") ?? [];

  return (
    <div className="space-y-8">
      <PageHeader
        title="Approvals"
        description="Every write action (e.g. creating a Jira ticket) waits here for an explicit human decision (UC-04)."
      />
      {isLoading && (
        <div className="space-y-4" aria-busy="true">
          <Skeleton className="h-56 rounded-xl" />
          <Skeleton className="h-40 rounded-xl" />
        </div>
      )}
      {error && <ErrorState error={error} onRetry={() => void refetch()} />}
      {data && (
        <>
          <section aria-labelledby="pending-title" className="space-y-3">
            <h2 id="pending-title" className="text-sm font-medium text-muted-foreground">
              Pending · {pending.length}
            </h2>
            {pending.length === 0 ? (
              <EmptyState
                icon={ShieldCheckIcon}
                title="Nothing waiting for approval"
                action={
                  <Button asChild variant="outline" size="sm">
                    <Link href="/investigations">Browse investigations</Link>
                  </Button>
                }
              >
                Write actions (like a Jira ticket from a report) wait here for your decision.
              </EmptyState>
            ) : (
              <Stagger className="space-y-4">
                {pending.map((a) => (
                  <StaggerItem key={a.id}>
                    <ApprovalCard approval={a} />
                  </StaggerItem>
                ))}
              </Stagger>
            )}
          </section>
          {decided.length > 0 && (
            <section aria-labelledby="history-title" className="space-y-3">
              <h2 id="history-title" className="text-sm font-medium text-muted-foreground">
                History · {decided.length}
              </h2>
              <Stagger className="space-y-4">
                {decided.map((a) => (
                  <StaggerItem key={a.id}>
                    <ApprovalCard approval={a} />
                  </StaggerItem>
                ))}
              </Stagger>
            </section>
          )}
        </>
      )}
    </div>
  );
}
