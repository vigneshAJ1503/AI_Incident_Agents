"use client";

import { InboxIcon, RotateCcwIcon, TriangleAlertIcon, type LucideIcon } from "lucide-react";
import type * as React from "react";

import { Button } from "@/components/ui/button";
import { ApiError, ContractError } from "@/lib/api/client";
import { cn } from "@/lib/utils";

export function describeError(error: unknown): { title: string; detail: string } {
  if (error instanceof ContractError) {
    return { title: "The API returned data this UI doesn't understand", detail: error.message };
  }
  if (error instanceof ApiError) {
    if (error.status === 0) return { title: "Can't reach the API", detail: error.message };
    if (error.status === 404) return { title: "Not found", detail: error.message };
    return { title: `Request failed (${error.status})`, detail: error.message };
  }
  return {
    title: "Something went wrong",
    detail: error instanceof Error ? error.message : String(error),
  };
}

export function ErrorState({
  error,
  onRetry,
  className,
}: {
  error: unknown;
  onRetry?: () => void;
  className?: string;
}) {
  const { title, detail } = describeError(error);
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col items-center gap-3 rounded-xl border border-dashed border-danger/40 bg-glass p-8 text-center",
        className,
      )}
    >
      <TriangleAlertIcon aria-hidden className="size-8 text-danger" />
      <div>
        <p className="font-medium">{title}</p>
        <p className="mt-1 max-w-lg text-sm break-words text-muted-foreground">{detail}</p>
      </div>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RotateCcwIcon /> Retry
        </Button>
      )}
    </div>
  );
}

/** A small decorative illustration: the icon on a glass tile with orbiting accents. */
function EmptyIllustration({ icon: Icon }: { icon: LucideIcon }) {
  return (
    <div aria-hidden className="relative mb-2 grid size-20 place-items-center">
      <svg viewBox="0 0 80 80" className="absolute inset-0 size-full text-primary">
        <circle
          cx="40"
          cy="40"
          r="37"
          fill="none"
          stroke="currentColor"
          strokeOpacity="0.18"
          strokeDasharray="3 6"
        />
        <circle cx="12" cy="22" r="3" fill="currentColor" fillOpacity="0.35" />
        <circle cx="70" cy="58" r="2.5" fill="currentColor" fillOpacity="0.25" />
        <circle cx="64" cy="14" r="1.8" fill="currentColor" fillOpacity="0.3" />
      </svg>
      <span className="grid size-12 place-items-center rounded-2xl glass">
        <Icon className="size-6 text-primary" />
      </span>
    </div>
  );
}

export function EmptyState({
  icon = InboxIcon,
  title,
  children,
  action,
  className,
}: {
  icon?: LucideIcon;
  title: string;
  children?: React.ReactNode;
  /** The next step (a button or link). */
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      data-testid="empty-state"
      className={cn(
        "flex flex-col items-center gap-2 rounded-xl border border-dashed border-glass-border bg-glass/50 p-10 text-center",
        className,
      )}
    >
      <EmptyIllustration icon={icon} />
      <p className="font-medium">{title}</p>
      {children && <div className="max-w-md text-sm text-muted-foreground">{children}</div>}
      {action && <div className="mt-2 flex flex-wrap justify-center gap-2">{action}</div>}
    </div>
  );
}
