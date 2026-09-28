"use client";

import { AnimatePresence, LayoutGroup, motion } from "framer-motion";
import { TerminalIcon } from "lucide-react";

import { StepBadge } from "@/components/status";
import { formatDuration, humanizeSignal } from "@/lib/format";
import type { Lane } from "@/lib/live/reducer";
import { cn } from "@/lib/utils";

import { agentMeta } from "./agent-meta";

const ring: Record<Lane["phase"], string> = {
  queued: "border-border",
  running: "border-info/60 shadow-[0_0_0_3px] shadow-info/15",
  done: "border-ok/40",
  failed: "border-danger/60",
  skipped: "border-border opacity-70",
  cancelled: "border-border opacity-70",
};

function LaneCard({ lane }: { lane: Lane }) {
  const meta = agentMeta(lane.agent);
  const Icon = meta.icon;
  return (
    <motion.li
      layout
      initial={{ opacity: 0, y: 10, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ type: "spring", stiffness: 420, damping: 34 }}
      data-testid={`lane-${lane.agent}-r${lane.round}`}
      data-phase={lane.phase}
      className={cn(
        "relative flex flex-col gap-2.5 rounded-xl border bg-card p-4 transition-[border-color,box-shadow]",
        ring[lane.phase],
      )}
      aria-label={`${meta.label}: ${lane.phase}`}
    >
      <div className="flex items-center gap-2.5">
        <span className="relative grid size-8 place-items-center rounded-lg bg-muted">
          <Icon aria-hidden className="size-4" />
          {lane.phase === "running" && (
            <span
              aria-hidden
              className="absolute inset-0 animate-ping rounded-lg bg-info/25 motion-reduce:hidden"
            />
          )}
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">{meta.label}</p>
          <p className="truncate text-[11px] text-muted-foreground">{meta.tool}</p>
        </div>
        <StepBadge status={lane.phase} />
      </div>
      <p className="line-clamp-2 text-xs text-muted-foreground">{lane.summary ?? lane.objective}</p>
      {lane.phase === "running" && (
        <div className="space-y-1.5">
          <div className="h-1 overflow-hidden rounded-full bg-muted" aria-hidden>
            <div className="h-full w-1/3 animate-[lane-progress_1.4s_ease-in-out_infinite] rounded-full bg-info motion-reduce:w-full motion-reduce:animate-none motion-reduce:opacity-40" />
          </div>
          <AnimatePresence mode="wait">
            {lane.currentTool && (
              <motion.p
                key={`${lane.currentTool}-${lane.tools.length}`}
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                className="flex items-center gap-1.5 font-mono text-[11px] text-info"
              >
                <TerminalIcon aria-hidden className="size-3" />
                {lane.currentTool}
              </motion.p>
            )}
          </AnimatePresence>
        </div>
      )}
      {lane.signals.length > 0 && (
        <ul className="flex flex-wrap gap-1" aria-label="Signals">
          <AnimatePresence>
            {lane.signals.map((sig, i) => (
              <motion.li
                key={sig}
                initial={{ opacity: 0, scale: 0.8 }}
                animate={{ opacity: 1, scale: 1 }}
                transition={{ delay: i * 0.06 }}
                className="rounded-full border bg-muted/60 px-2 py-0.5 text-[11px]"
              >
                {humanizeSignal(sig)}
              </motion.li>
            ))}
          </AnimatePresence>
        </ul>
      )}
      <p className="mt-auto flex gap-3 text-[11px] text-muted-foreground tabular-nums">
        <span>{lane.tools.length} tool calls</span>
        <span>{lane.evidenceCount} evidence</span>
        {lane.durationMs !== null && <span>{formatDuration(lane.durationMs)}</span>}
      </p>
    </motion.li>
  );
}

/** One card per step, grouped by round, re-laid-out as agents change state. */
export function AgentLane({ lanes, rounds }: { lanes: Lane[]; rounds: number[] }) {
  const byRound = rounds.length ? rounds : [...new Set(lanes.map((l) => l.round))].sort();
  return (
    <LayoutGroup>
      <div className="space-y-5">
        {byRound.map((r) => {
          const inRound = lanes.filter((l) => l.round === r);
          if (inRound.length === 0) return null;
          return (
            <section key={r} aria-label={`Round ${r}`}>
              <h3 className="mb-2 flex items-center gap-2 text-xs font-medium text-muted-foreground">
                Round {r}
                <span className="font-normal">{r === 1 ? "· parallel" : "· follow-ups"}</span>
              </h3>
              <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                {inRound.map((l) => (
                  <LaneCard key={l.stepId} lane={l} />
                ))}
              </ul>
            </section>
          );
        })}
      </div>
    </LayoutGroup>
  );
}
