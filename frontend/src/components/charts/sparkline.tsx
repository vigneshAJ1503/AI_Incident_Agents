import { useId, useMemo } from "react";

import { cn } from "@/lib/utils";

/**
 * A tiny trend line (inline SVG, no chart library, no layout shift: fixed height). Decorative:
 * the tile states the numbers, the SVG only carries an accessible summary via `label`.
 */
export function Sparkline({
  values,
  color = "var(--chart-1)",
  label,
  className,
  height = 32,
}: {
  values: number[];
  color?: string;
  label: string;
  className?: string;
  height?: number;
}) {
  const gid = `spark-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const { line, area, last } = useMemo(() => {
    const w = 100;
    const n = Math.max(values.length, 2);
    const max = Math.max(1, ...values);
    const pts = (values.length ? values : [0, 0]).map((v, i) => [
      (i / (n - 1)) * w,
      height - 2 - (v / max) * (height - 6),
    ]);
    const line = pts
      .map(([x, y], i) => `${i ? "L" : "M"}${x!.toFixed(2)},${y!.toFixed(2)}`)
      .join("");
    return {
      line,
      area: `${line}L${w},${height}L0,${height}Z`,
      last: pts.at(-1)!,
    };
  }, [values, height]);

  return (
    <svg
      role="img"
      aria-label={label}
      viewBox={`0 0 100 ${height}`}
      preserveAspectRatio="none"
      className={cn("block w-full overflow-visible", className)}
      style={{ height }}
    >
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={color} stopOpacity="0.28" />
          <stop offset="1" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${gid})`} />
      <path
        d={line}
        fill="none"
        stroke={color}
        strokeWidth="1.75"
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
      <circle cx={last[0]} cy={last[1]} r="2.2" fill={color} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
