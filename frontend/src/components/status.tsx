import {
  BanIcon,
  CircleCheckIcon,
  CircleDashedIcon,
  CircleHelpIcon,
  CircleXIcon,
  ClockIcon,
  FlameIcon,
  InfoIcon,
  LoaderCircleIcon,
  OctagonAlertIcon,
  TriangleAlertIcon,
  type LucideIcon,
} from "lucide-react";

import { Badge, type Tone } from "@/components/ui/badge";
import type { InvestigationStatus, Severity, StepStatus } from "@/lib/api/schemas";
import { cn } from "@/lib/utils";

type Meta = { label: string; tone: Tone; icon: LucideIcon; spin?: boolean };

export const STATUS_META: Record<InvestigationStatus, Meta> = {
  pending: { label: "Pending", tone: "neutral", icon: ClockIcon },
  running: { label: "Running", tone: "info", icon: LoaderCircleIcon, spin: true },
  needs_clarification: { label: "Needs input", tone: "purple", icon: CircleHelpIcon },
  completed: { label: "Completed", tone: "ok", icon: CircleCheckIcon },
  partial: { label: "Partial", tone: "warn", icon: TriangleAlertIcon },
  failed: { label: "Failed", tone: "danger", icon: CircleXIcon },
  cancelled: { label: "Cancelled", tone: "neutral", icon: BanIcon },
};

export const SEVERITY_META: Record<Severity, Meta> = {
  critical: { label: "Critical", tone: "danger", icon: FlameIcon },
  high: { label: "High", tone: "warn", icon: OctagonAlertIcon },
  medium: { label: "Medium", tone: "purple", icon: TriangleAlertIcon },
  low: { label: "Low", tone: "info", icon: InfoIcon },
  none: { label: "None", tone: "neutral", icon: CircleCheckIcon },
};

export const STEP_META: Record<StepStatus | "cancelled", Meta> = {
  queued: { label: "Queued", tone: "neutral", icon: CircleDashedIcon },
  running: { label: "Running", tone: "info", icon: LoaderCircleIcon, spin: true },
  done: { label: "Done", tone: "ok", icon: CircleCheckIcon },
  failed: { label: "Failed", tone: "danger", icon: CircleXIcon },
  skipped: { label: "Skipped", tone: "neutral", icon: BanIcon },
  cancelled: { label: "Cancelled", tone: "neutral", icon: BanIcon },
};

function MetaBadge({ meta, className }: { meta: Meta; className?: string }) {
  const Icon = meta.icon;
  return (
    <Badge tone={meta.tone} className={className}>
      <Icon aria-hidden className={cn(meta.spin && "animate-spin motion-reduce:animate-none")} />
      {meta.label}
    </Badge>
  );
}

export function StatusBadge({
  status,
  className,
}: {
  status: InvestigationStatus;
  className?: string;
}) {
  return <MetaBadge meta={STATUS_META[status]} className={className} />;
}

export function SeverityBadge({ severity, className }: { severity: Severity; className?: string }) {
  return <MetaBadge meta={SEVERITY_META[severity]} className={className} />;
}

export function StepBadge({
  status,
  className,
}: {
  status: StepStatus | "cancelled";
  className?: string;
}) {
  return <MetaBadge meta={STEP_META[status]} className={className} />;
}

const confTone = (c: number) => (c >= 0.8 ? "bg-ok" : c >= 0.5 ? "bg-warn" : "bg-danger");

/** Horizontal confidence meter; `null`/0 renders as "n/a". */
export function ConfidenceMeter({
  value,
  className,
}: {
  value: number | null | undefined;
  className?: string;
}) {
  if (!value) return <span className={cn("text-xs text-muted-foreground", className)}>n/a</span>;
  const pct = Math.round(value * 100);
  return (
    <div className={cn("flex items-center gap-2", className)}>
      <div
        role="meter"
        aria-label="Confidence"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        className="h-1.5 w-16 overflow-hidden rounded-full bg-muted"
      >
        <div
          className={cn("h-full rounded-full transition-[width] duration-700", confTone(value))}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-xs font-medium tabular-nums">{pct}%</span>
    </div>
  );
}
