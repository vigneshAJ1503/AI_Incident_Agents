"use client";

import {
  CircleCheckIcon,
  CircleDashedIcon,
  CircleMinusIcon,
  CircleXIcon,
  KeyRoundIcon,
  MessageSquareIcon,
  PencilLineIcon,
  SlidersHorizontalIcon,
  type LucideIcon,
} from "lucide-react";
import type * as React from "react";

import { agentMeta } from "@/components/investigation/agent-meta";
import { Badge, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import type { Integration, IntegrationStatus } from "@/lib/api/schemas";
import { formatRelative } from "@/lib/format";
import { CAPABILITY_INFO, capabilityTitle, maskedSecret } from "@/lib/integrations";

export const INTEGRATION_STATUS: Record<
  IntegrationStatus,
  { label: string; tone: Tone; icon: LucideIcon }
> = {
  ok: { label: "Connected", tone: "ok", icon: CircleCheckIcon },
  down: { label: "Unreachable", tone: "danger", icon: CircleXIcon },
  disabled: { label: "Disabled", tone: "neutral", icon: CircleMinusIcon },
  not_configured: { label: "Not configured", tone: "neutral", icon: CircleDashedIcon },
};

export function IntegrationStatusBadge({ status }: { status: IntegrationStatus }) {
  const meta = INTEGRATION_STATUS[status];
  return (
    <Badge tone={meta.tone}>
      <meta.icon aria-hidden />
      {meta.label}
    </Badge>
  );
}

function CardShell({
  icon: Icon,
  title,
  subtitle,
  aside,
  children,
  testId,
}: {
  icon: LucideIcon;
  title: string;
  subtitle: string;
  aside: React.ReactNode;
  children: React.ReactNode;
  testId: string;
}) {
  return (
    <Card className="h-full" data-testid={testId}>
      <CardContent className="flex h-full flex-col gap-3 pt-(--pad)">
        <div className="flex items-start gap-3">
          <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-muted">
            <Icon aria-hidden className="size-4.5" />
          </span>
          <div className="min-w-0 flex-1">
            <h2 className="text-sm font-semibold">{title}</h2>
            <p className="truncate text-xs text-muted-foreground">{subtitle}</p>
          </div>
          {aside}
        </div>
        {children}
      </CardContent>
    </Card>
  );
}

export function IntegrationCard({
  item,
  onConfigure,
}: {
  item: Integration;
  onConfigure: (capability: string) => void;
}) {
  const meta = agentMeta(item.capability);
  const title = capabilityTitle(item.capability);
  const url = item.fields.find((f) => f.key === "mcp.url")?.value;
  const overrides = item.overridden.length;
  return (
    <CardShell
      icon={meta.icon}
      title={title}
      subtitle={CAPABILITY_INFO[item.capability]?.vendors ?? "custom capability"}
      aside={<IntegrationStatusBadge status={item.status} />}
      testId={`integration-${item.capability}`}
    >
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-xs">
        <dt className="text-muted-foreground">Provider</dt>
        <dd>
          {item.provider ? (
            <Badge tone="info" className="font-mono">
              {item.provider}
            </Badge>
          ) : (
            <span className="text-muted-foreground">none</span>
          )}
        </dd>
        <dt className="text-muted-foreground">Server</dt>
        <dd className="truncate font-mono" title={typeof url === "string" ? url : undefined}>
          {typeof url === "string" && url ? url : item.transport === "stdio" ? "stdio" : "–"}
        </dd>
        <dt className="text-muted-foreground">Secrets</dt>
        <dd className="flex flex-wrap gap-1">
          {item.secrets.length === 0 && <span className="text-muted-foreground">none</span>}
          {item.secrets.map((s) => (
            <Badge
              key={s.name}
              tone={s.usable ? "neutral" : "warn"}
              className="font-mono"
              data-testid={`secret-${item.capability}-${s.name}`}
            >
              <KeyRoundIcon aria-hidden />
              {s.name}: {maskedSecret(s)}
            </Badge>
          ))}
        </dd>
      </dl>
      {item.notes.length > 0 && (
        <p className="rounded-md bg-warn-bg px-2 py-1 text-xs text-warn">{item.notes[0]}</p>
      )}
      <div className="mt-auto flex items-center gap-2 border-t pt-3">
        <span
          className="min-w-0 flex-1 truncate text-xs text-muted-foreground"
          title={
            item.updated_by
              ? `Last changed by ${item.updated_by} ${formatRelative(item.updated_at)}`
              : undefined
          }
        >
          {overrides > 0 ? (
            <>
              <PencilLineIcon aria-hidden className="mr-1 inline size-3.5" />
              {overrides} UI override{overrides > 1 ? "s" : ""}
              {item.updated_by && ` · ${item.updated_by}`}
            </>
          ) : (
            "Profile defaults"
          )}
        </span>
        <Button
          size="sm"
          variant="outline"
          disabled={!item.configured}
          onClick={() => onConfigure(item.capability)}
          aria-label={`Configure ${title}`}
        >
          <SlidersHorizontalIcon aria-hidden />
          Configure
        </Button>
      </div>
    </CardShell>
  );
}

/** Slack/Teams: shown so the page lists every channel on the roadmap. */
export function SlackPlaceholderCard() {
  return (
    <CardShell
      icon={MessageSquareIcon}
      title="Chat (Slack)"
      subtitle="Slack · Teams"
      aside={<Badge tone="purple">Coming in PR-048</Badge>}
      testId="integration-slack"
    >
      <p className="text-xs text-muted-foreground">
        <code className="font-mono">/investigate</code>, threaded progress and approval buttons
        arrive with PR-048. Nothing to configure yet.
      </p>
    </CardShell>
  );
}
