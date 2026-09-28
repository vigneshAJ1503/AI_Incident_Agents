"use client";

import { AnimatePresence, LayoutGroup, motion } from "framer-motion";
import { TerminalIcon } from "lucide-react";
import { memo } from "react";

import { StepBadge } from "@/components/status";
import { formatDuration, humanizeSignal } from "@/lib/format";
import type { Lane } from "@/lib/live/reducer";
import { cn } from "@/lib/utils";

import { agentMeta } from "./agent-meta";

const ring: Record<Lane["phase"], string> = {
  queued: "",
  running: "border-info/50 shadow-[0_0_0_3px_color-mix(in_oklch,var(--info)_14%,transparent)]",
  done: "border-ok/35",
  failed: "border-danger/55",
  skipped: "opacity-70",
  cancelled: "opacity-70",
};

const RING_COLOR: Record<Lane["phase"], string> = {
  queued: "var(--muted-foreground)",
  running: "var(--info)",
  done: "var(--ok)",
  failed: "var(--danger)",
  skipped: "var(--muted-foreground)",
  cancelled: "var(--muted-foreground)",
};

/**
 * The agent's icon inside a progress ring: a spinning arc with a soft pulse while running, a full
 * ring when done/failed, dashed while queued. Transform/opacity animations only; static (a full
 * faint ring) under prefers-reduced-motion.
 */
function ProgressRing({ lane }: { lane: Lane }) {
  const meta = agentMeta(lane.agent);
  const Icon = meta.icon;
  const color = RING_COLOR[lane.phase];
  const running = lane.phase === "running";
  const full = lane.phase === "done" || lane.phase === "failed";
  const r = 17;
  const c = 2 * Math.PI * r;
  return (
    <span className="relative grid size-10 shrink-0 place-items-center" aria-hidden>
      {running && (
        <span
          className="absolute inset-0 animate-ping rounded-full opacity-20 motion-reduce:hidden"
          style={{ background: color }}
        />
      )}
      <svg viewBox="0 0 40 40" className="absolute inset-0 size-full">
        <circle cx="20" cy="20" r={r} fill="none" stroke="var(--border)" strokeWidth="2.5" />
        <circle
          cx="20"
          cy="20"
          r={r}
          fill="none"
          stroke={color}
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeDasharray={full ? undefined : running ? `${c * 0.28} ${c}` : "2 4"}
          strokeOpacity={lane.phase === "queued" ? 0.5 : 1}
          className={cn(
            "origin-center transition-[stroke] duration-300",
            running &&
              "animate-[ring-spin_1.1s_linear_infinite] motion-reduce:animate-none motion-reduce:[stroke-dasharray:none] motion-reduce:[stroke-opacity:0.45]",
          )}
          style={{ transformBox: "fill-box" }}
        />
      </svg>
      <span className="grid size-7 place-items-center rounded-full bg-card/80">
        <Icon className="size-3.5" />
      </span>
    </span>
  );
}

const LaneCard = memo(function LaneCard({ lane }: { lane: Lane }) {
  const meta = agentMeta(lane.agent);
  return (
    <motion.li
      layout
      initial={{ opacity: 0, y: 10, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ type: "spring", stiffness: 420, damping: 34 }}
      data-testid={`lane-${lane.agent}-r${lane.round}`}
      data-phase={lane.phase}
      className={cn(
        "relative flex flex-col gap-2.5 rounded-xl glass p-4 transition-[border-color,box-shadow,opacity]",
        ring[lane.phase],
      )}
      aria-label={`${meta.label}: ${lane.phase}`}
    >
      <div className="flex items-start gap-2.5">
        <ProgressRing lane={lane} />
        <div className="min-w-0 flex-1">
          {/* The full name wraps instead of truncating ("Kubernetes agent" in a narrow lane). */}
          <p className="text-sm leading-snug font-medium text-balance">{meta.label}</p>
          <p className="truncate text-[11px] text-muted-foreground" title={meta.tool}>
            {meta.tool}
          </p>
        </div>
        <StepBadge status={lane.phase} className="shrink-0" />
      </div>
      <p className="line-clamp-2 text-xs text-muted-foreground">{lane.summary ?? lane.objective}</p>
      {lane.phase === "running" && (
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
                className="rounded-full border border-glass-border bg-muted/60 px-2 py-0.5 text-[11px]"
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
});

/** One glass card per step, grouped by round, re-laid-out as agents change state. */
export function AgentLane({ lanes, rounds }: { lanes: Lane[]; rounds: number[] }) {
  const byRound = rounds.length ? rounds : [...new Set(lanes.map((l) => l.round))].sort();
  return (
    <LayoutGroup>
      <div className="space-y-5">
        {byRound.map((r) => {
          const inRound = lanes.filter((l) => l.round === r);
          if (inRound.length === 0) return null;
          const done = inRound.filter((l) => l.phase === "done").length;
          return (
            <section key={r} aria-label={`Round ${r}`}>
              <h3 className="mb-2 flex items-center gap-2 text-xs font-medium text-muted-foreground">
                Round {r}
                <span className="font-normal">{r === 1 ? "· parallel" : "· follow-ups"}</span>
                <span className="ml-auto font-normal tabular-nums">
                  {done}/{inRound.length} done
                </span>
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
