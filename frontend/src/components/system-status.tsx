"use client";

import {
  CircleCheckIcon,
  CircleMinusIcon,
  CircleXIcon,
  FlaskConicalIcon,
  KeyRoundIcon,
  ServerCrashIcon,
} from "lucide-react";

import type * as React from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip } from "@/components/ui/tooltip";
import type { CapabilityState } from "@/lib/api/schemas";
import { DEMO_MODE } from "@/lib/config";
import { useHealth } from "@/lib/queries";
import { cn } from "@/lib/utils";

/** Global banner: demo mode, no LLM key (replay mode) or API unreachable. */
export function ModeBanner() {
  const health = useHealth();
  let content: React.ReactNode = null;
  let tone = "bg-info-bg text-info";
  if (DEMO_MODE) {
    content = (
      <>
        <FlaskConicalIcon aria-hidden className="size-4" />
        <span>
          <strong className="font-semibold">Demo mode</strong>: a static dataset with simulated live
          investigations. No backend, no LLM, zero tokens.
        </span>
      </>
    );
  } else if (health.isError) {
    tone = "bg-danger-bg text-danger";
    content = (
      <>
        <ServerCrashIcon aria-hidden className="size-4" />
        <span>
          <strong className="font-semibold">API unreachable.</strong> Start the backend or run the
          UI with NEXT_PUBLIC_DEMO=1.
        </span>
      </>
    );
  } else if (health.data && !health.data.llm.configured) {
    tone = "bg-warn-bg text-warn";
    content = (
      <>
        <KeyRoundIcon aria-hidden className="size-4" />
        <span>
          <strong className="font-semibold">No LLM key</strong>: investigations run in replay mode
          (recorded fixtures, fake LLM).
        </span>
      </>
    );
  }
  if (!content) return null;
  return (
    <div
      role="status"
      data-testid="mode-banner"
      className={cn("flex items-center gap-2 px-4 py-2 text-xs md:px-6", tone)}
    >
      {content}
    </div>
  );
}

const CAP_META: Record<
  CapabilityState,
  { icon: typeof CircleCheckIcon; cls: string; label: string }
> = {
  ok: { icon: CircleCheckIcon, cls: "text-ok", label: "up" },
  down: { icon: CircleXIcon, cls: "text-danger", label: "down" },
  disabled: { icon: CircleMinusIcon, cls: "text-muted-foreground", label: "disabled" },
};

/** Dashboard strip: capabilities up/down, LLM, profile, version (GET /health). */
export function SystemStatusStrip() {
  const { data, isLoading, isError } = useHealth();
  if (isLoading) return <Skeleton className="h-11 w-full rounded-xl" />;
  if (isError || !data) {
    return (
      <div
        role="status"
        className="flex items-center gap-2 rounded-xl border border-dashed px-4 py-3 text-sm text-danger"
      >
        <CircleXIcon aria-hidden className="size-4" /> System status unavailable (GET /health
        failed)
      </div>
    );
  }
  return (
    <section
      aria-label="System status"
      className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border bg-card px-4 py-2.5 text-xs shadow-xs"
    >
      <span className="flex items-center gap-1.5 font-medium">
        <span
          aria-hidden
          className={cn(
            "size-2 rounded-full",
            data.status === "ok" ? "bg-ok" : "bg-warn",
            "animate-pulse motion-reduce:animate-none",
          )}
        />
        System {data.status}
      </span>
      <span className="text-muted-foreground">
        profile <span className="font-mono text-foreground">{data.profile}</span> · v{data.version}
      </span>
      <span className="hidden h-4 w-px bg-border sm:block" />
      <ul className="flex flex-wrap items-center gap-x-3 gap-y-1" aria-label="Capabilities">
        {Object.entries(data.capabilities).map(([cap, state]) => {
          const m = CAP_META[state];
          return (
            <li key={cap}>
              <Tooltip content={`${cap}: ${m.label}`}>
                <span className="flex items-center gap-1" tabIndex={0}>
                  <m.icon aria-hidden className={cn("size-3.5", m.cls)} />
                  <span>{cap}</span>
                  <span className="sr-only">{m.label}</span>
                </span>
              </Tooltip>
            </li>
          );
        })}
      </ul>
      <span className="ml-auto flex items-center gap-1.5">
        <KeyRoundIcon
          aria-hidden
          className={cn("size-3.5", data.llm.configured ? "text-ok" : "text-warn")}
        />
        LLM{" "}
        {data.llm.configured
          ? `configured (${data.llm.provider ?? "unknown"})`
          : "not configured: replay mode"}
      </span>
    </section>
  );
}
