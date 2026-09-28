"use client";

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatClock, formatMetric } from "@/lib/format";
import type { MetricData } from "@/lib/evidence";

const COLORS = ["var(--chart-1)", "var(--chart-4)", "var(--chart-3)", "var(--chart-2)"];

/** Metric series with the anomaly window shaded (ReferenceArea). */
export function MetricChart({ data, height = 200 }: { data: MetricData; height?: number }) {
  const rows = new Map<number, Record<string, number>>();
  data.series.forEach((s, i) => {
    for (const p of s.points) {
      const t = Date.parse(p.t);
      const row = rows.get(t) ?? { t };
      row[`s${i}`] = p.v;
      rows.set(t, row);
    }
  });
  const points = [...rows.values()].sort((a, b) => (a.t ?? 0) - (b.t ?? 0));
  const unit = data.unit;
  const a = data.anomaly;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart
        data={points}
        margin={{ top: 8, right: 12, bottom: 0, left: 0 }}
        accessibilityLayer
      >
        <CartesianGrid vertical={false} stroke="var(--border)" />
        <XAxis
          dataKey="t"
          type="number"
          scale="time"
          domain={["dataMin", "dataMax"]}
          tickFormatter={(t: number) => formatClock(new Date(t).toISOString()).slice(0, 5)}
          tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
          tickLine={false}
          axisLine={false}
          minTickGap={40}
        />
        <YAxis
          width={56}
          tickFormatter={(v: number) => formatMetric(v, unit)}
          tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
          tickLine={false}
          axisLine={false}
        />
        {a && (
          <ReferenceArea
            x1={Date.parse(a.start)}
            x2={Date.parse(a.end)}
            fill="var(--anomaly)"
            stroke="var(--chart-4)"
            strokeOpacity={0.35}
            strokeDasharray="3 3"
            label={{
              value: "anomaly",
              position: "insideTopLeft",
              fontSize: 10,
              fill: "var(--danger)",
            }}
          />
        )}
        <Tooltip
          contentStyle={{
            background: "var(--popover)",
            border: "1px solid var(--border)",
            borderRadius: 8,
            fontSize: 12,
            color: "var(--popover-foreground)",
          }}
          labelFormatter={(t) => `${formatClock(new Date(Number(t)).toISOString())} UTC`}
          formatter={(v) => formatMetric(Number(v), unit)}
        />
        {data.series.length > 1 && <Legend iconType="plainline" wrapperStyle={{ fontSize: 12 }} />}
        {data.series.map((s, i) => (
          <Line
            key={s.label}
            dataKey={`s${i}`}
            name={s.label}
            type="monotone"
            stroke={COLORS[i % COLORS.length]}
            strokeWidth={2}
            dot={false}
            isAnimationActive
            animationDuration={700}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
