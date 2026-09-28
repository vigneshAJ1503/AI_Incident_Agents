/** Pure formatting helpers (unit-tested in format.test.ts). */

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 86_400_000],
  ["month", 30 * 86_400_000],
  ["week", 7 * 86_400_000],
  ["day", 86_400_000],
  ["hour", 3_600_000],
  ["minute", 60_000],
  ["second", 1000],
];

const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const diff = t - now;
  const abs = Math.abs(diff);
  if (abs < 45_000) return "just now";
  for (const [unit, ms] of UNITS) {
    if (abs >= ms || unit === "second") return rtf.format(Math.round(diff / ms), unit);
  }
  return "just now";
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)} s`;
  const m = Math.floor(s / 60);
  const rs = Math.round(s % 60);
  if (m < 60) return rs ? `${m} min ${rs} s` : `${m} min`;
  const h = Math.floor(m / 60);
  return `${h} h ${m % 60} min`;
}

export function formatPercent(ratio: number | null | undefined, digits = 0): string {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return "—";
  return `${(ratio * 100).toFixed(digits)}%`;
}

/** 2026-09-28T10:10:07Z → "10:10:07" (UTC, as incident timelines are). */
export function formatClock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toISOString().slice(11, 19);
}

/** Absolute date-time in UTC, e.g. "Sep 28, 10:30 UTC". */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" })} UTC`;
}

const ACRONYMS = new Set([
  "db",
  "oom",
  "p95",
  "p99",
  "http",
  "k8s",
  "api",
  "cpu",
  "rss",
  "sla",
  "slo",
]);

/** "db_timeout_errors_up" → "DB timeout errors up". */
export function humanizeSignal(signal: string): string {
  const words = signal.split(/[_\s]+/).filter(Boolean);
  return words
    .map((w, i) => {
      if (ACRONYMS.has(w.toLowerCase())) return w.toUpperCase();
      return i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w;
    })
    .join(" ");
}

export function formatCompact(n: number): string {
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

/** Value with its metric unit, as metric evidence reports it. */
export function formatMetric(value: number, unit: string): string {
  switch (unit) {
    case "percent":
      return `${value < 10 ? value.toFixed(1) : Math.round(value)}%`;
    case "seconds":
      return value < 1
        ? `${Math.round(value * 1000)} ms`
        : `${value.toFixed(value < 10 ? 2 : 1)} s`;
    case "rps":
      return `${value.toFixed(1)} req/s`;
    case "bytes":
      return `${Math.round(value)} Mi`;
    case "bool":
      return value >= 0.5 ? "up" : "down";
    default:
      return formatCompact(value);
  }
}
