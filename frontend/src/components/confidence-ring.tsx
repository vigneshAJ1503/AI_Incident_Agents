"use client";

import { motion, useReducedMotion } from "framer-motion";

import { cn } from "@/lib/utils";

/** Animated confidence ring (0..1). */
export function ConfidenceRing({
  value,
  size = 96,
  className,
}: {
  value: number;
  size?: number;
  className?: string;
}) {
  const reduce = useReducedMotion();
  const stroke = 8;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const pct = Math.round(value * 100);
  const color = value >= 0.8 ? "var(--ok)" : value >= 0.5 ? "var(--warn)" : "var(--danger)";
  return (
    <div
      className={cn("relative grid place-items-center", className)}
      style={{ width: size, height: size }}
      role="meter"
      aria-label="Root-cause confidence"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct}
    >
      <svg width={size} height={size} className="-rotate-90" aria-hidden>
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke="var(--muted)"
          strokeWidth={stroke}
        />
        <motion.circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={color}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={c}
          initial={{ strokeDashoffset: reduce ? c * (1 - value) : c }}
          animate={{ strokeDashoffset: c * (1 - value) }}
          transition={{ duration: 1.1, ease: [0.16, 1, 0.3, 1] }}
        />
      </svg>
      <span className="absolute text-center leading-none">
        <span className="block text-2xl font-semibold tabular-nums" data-testid="confidence">
          {pct}%
        </span>
        <span className="text-[10px] text-muted-foreground">confidence</span>
      </span>
    </div>
  );
}
