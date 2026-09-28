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
        "flex flex-col items-center gap-3 rounded-xl border border-dashed p-8 text-center",
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

export function EmptyState({
  icon: Icon = InboxIcon,
  title,
  children,
  className,
}: {
  icon?: LucideIcon;
  title: string;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center gap-2 rounded-xl border border-dashed p-10 text-center",
        className,
      )}
    >
      <Icon aria-hidden className="size-8 text-muted-foreground" />
      <p className="font-medium">{title}</p>
      {children && <div className="text-sm text-muted-foreground">{children}</div>}
    </div>
  );
}
