"use client";

import { motion, useReducedMotion } from "framer-motion";
import { useId } from "react";

import { cn } from "@/lib/utils";

/**
 * Confidence ring (0..1): the arc fills in, and its gradient stroke slowly rotates (a transform on
 * the gradient only, cheap). Both are static under prefers-reduced-motion. The number, not the
 * colour, carries the meaning; the band colour (ok/warn/danger) follows the existing thresholds.
 */
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
  const gid = `ring-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const stroke = 8;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const mid = size / 2;
  const pct = Math.round(value * 100);
  const band = value >= 0.8 ? "var(--ok)" : value >= 0.5 ? "var(--warn)" : "var(--danger)";
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
      {/* soft halo in the band colour */}
      <span
        aria-hidden
        className="absolute -inset-2 rounded-full opacity-30"
        style={{ background: `radial-gradient(closest-side, ${band}, transparent)` }}
      />
      <svg width={size} height={size} className="relative -rotate-90" aria-hidden>
        <defs>
          <motion.linearGradient
            id={gid}
            gradientUnits="userSpaceOnUse"
            x1="0"
            y1="0"
            x2={size}
            y2={size}
            initial={{ gradientTransform: `rotate(0 ${mid} ${mid})` }}
            animate={reduce ? undefined : { gradientTransform: `rotate(360 ${mid} ${mid})` }}
            transition={{ duration: 8, ease: "linear", repeat: Infinity, delay: 1.2 }}
          >
            <stop offset="0" stopColor={band} />
            <stop offset="0.55" stopColor="var(--primary)" />
            <stop offset="1" stopColor={band} />
          </motion.linearGradient>
        </defs>
        <circle
          cx={mid}
          cy={mid}
          r={r}
          fill="none"
          stroke="var(--muted)"
          strokeOpacity={0.8}
          strokeWidth={stroke}
        />
        <motion.circle
          cx={mid}
          cy={mid}
          r={r}
          fill="none"
          stroke={`url(#${gid})`}
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
