"use client";

import { KeyRoundIcon, ShieldAlertIcon } from "lucide-react";
import { useState } from "react";

import { IntegrationCard, SlackPlaceholderCard } from "@/components/integrations/integration-card";
import { IntegrationDialog } from "@/components/integrations/integration-dialog";
import { Stagger, StaggerItem } from "@/components/motion";
import { PageHeader } from "@/components/page-header";
import { ErrorState } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useIntegrations } from "@/lib/queries";

export default function IntegrationsPage() {
  const { data, isLoading, error, refetch } = useIntegrations();
  const [editing, setEditing] = useState<string | null>(null);
  const item = data?.items.find((i) => i.capability === editing) ?? null;
  return (
    <div>
      <PageHeader
        title="Integrations"
        description={
          <>
            <span className="text-foreground/80">Settings</span> · connect each capability to your
            company&apos;s tools. Changes are saved on top of the profile and apply to new
            investigations without a restart.
          </>
        }
        actions={
          data && (
            <>
              <Badge tone="outline" className="font-mono">
                profile: {data.profile}
              </Badge>
              <Badge tone={data.secrets_enabled ? "ok" : "warn"}>
                <KeyRoundIcon aria-hidden />
                {data.secrets_enabled ? "Secrets encrypted" : "Secrets off"}
              </Badge>
            </>
          )
        }
      />
      {data && !data.secrets_enabled && (
        <p
          role="note"
          className="mb-4 flex items-center gap-2 rounded-lg bg-warn-bg px-3 py-2 text-xs text-warn"
        >
          <ShieldAlertIcon aria-hidden className="size-4 shrink-0" />
          Secrets can&apos;t be saved from the UI until AIOPS_SECRETS_KEY is set on the API.
          Non-secret settings still work.
        </p>
      )}
      {isLoading && (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3" aria-busy="true">
          {Array.from({ length: 8 }, (_, i) => (
            <Skeleton key={i} className="h-52 rounded-xl" />
          ))}
        </div>
      )}
      {error && <ErrorState error={error} onRetry={() => void refetch()} />}
      {data && (
        <Stagger className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {data.items.map((i) => (
            <StaggerItem key={i.capability}>
              <IntegrationCard item={i} onConfigure={setEditing} />
            </StaggerItem>
          ))}
          <StaggerItem>
            <SlackPlaceholderCard />
          </StaggerItem>
        </Stagger>
      )}
      <IntegrationDialog
        item={item}
        secretsEnabled={data?.secrets_enabled ?? false}
        open={item !== null}
        onOpenChange={(open) => !open && setEditing(null)}
      />
    </div>
  );
}
