"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { DashboardSummary } from "@/lib/api/schemas";

const SEV = [
  { key: "critical", label: "Critical", color: "var(--chart-4)" },
  { key: "high", label: "High", color: "var(--chart-2)" },
  { key: "medium", label: "Medium", color: "var(--chart-5)" },
  { key: "low", label: "Low", color: "var(--chart-1)" },
] as const;

const tooltipStyle = {
  background: "var(--popover)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  color: "var(--popover-foreground)",
  fontSize: 12,
};

const axis = { fontSize: 11, fill: "var(--muted-foreground)" };

export function PerDayChart({ data }: { data: DashboardSummary["by_day"] }) {
  const rows = data.map((d) => ({
    ...d,
    label: new Date(`${d.date}T00:00:00Z`).toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      timeZone: "UTC",
    }),
    none: Math.max(0, d.investigations - d.critical - d.high - d.medium - d.low),
  }));
  return (
    <ResponsiveContainer width="100%" height={240}>
      <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: -20 }} accessibilityLayer>
        <CartesianGrid vertical={false} stroke="var(--border)" />
        <XAxis
          dataKey="label"
          tick={axis}
          tickLine={false}
          axisLine={false}
          interval="preserveStartEnd"
          minTickGap={16}
        />
        <YAxis allowDecimals={false} tick={axis} tickLine={false} axisLine={false} />
        <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "var(--muted)" }} />
        <Legend iconType="circle" iconSize={8} itemSorter={null} wrapperStyle={{ fontSize: 12 }} />
        {SEV.map((s, i) => (
          <Bar
            key={s.key}
            dataKey={s.key}
            name={s.label}
            stackId="sev"
            fill={s.color}
            radius={i === SEV.length - 1 ? [3, 3, 0, 0] : 0}
          />
        ))}
        <Bar
          dataKey="none"
          name="No incident"
          stackId="sev"
          fill="var(--neutral)"
          fillOpacity={0.35}
          radius={[3, 3, 0, 0]}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}

export function ByServiceChart({ data }: { data: DashboardSummary["by_service"] }) {
  return (
    <ResponsiveContainer width="100%" height={240}>
      <BarChart
        data={data}
        layout="vertical"
        margin={{ top: 4, right: 16, bottom: 0, left: 8 }}
        accessibilityLayer
      >
        <CartesianGrid horizontal={false} stroke="var(--border)" />
        <XAxis type="number" allowDecimals={false} tick={axis} tickLine={false} axisLine={false} />
        <YAxis
          type="category"
          dataKey="service"
          width={112}
          tick={axis}
          tickLine={false}
          axisLine={false}
        />
        <Tooltip
          contentStyle={tooltipStyle}
          cursor={{ fill: "var(--muted)" }}
          formatter={(v, _n, item) => [
            `${String(v)} investigations`,
            (item.payload as { top_root_cause?: string | null }).top_root_cause ?? "no root cause",
          ]}
        />
        <Bar
          dataKey="investigations"
          name="Investigations"
          fill="var(--chart-1)"
          radius={[0, 4, 4, 0]}
          barSize={18}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}
