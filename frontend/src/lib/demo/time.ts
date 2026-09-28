const DAY = 86_400_000;
const ISO_TS = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** Whole days to add to the dataset so its newest item sits in the recent past (never in the future). */
export function dayShift(anchorIso: string, now: number): number {
  const anchor = Date.parse(anchorIso);
  return Math.max(0, Math.floor((now - anchor) / DAY)) * DAY;
}

function shiftIso(value: string, deltaMs: number): string {
  if (ISO_TS.test(value)) return new Date(Date.parse(value) + deltaMs).toISOString();
  if (ISO_DATE.test(value)) {
    return new Date(Date.parse(`${value}T00:00:00Z`) + Math.round(deltaMs / DAY) * DAY)
      .toISOString()
      .slice(0, 10);
  }
  return value;
}

/** Deep-copy `value`, moving every ISO timestamp/date string by `deltaMs`. */
export function shiftTimes<T>(value: T, deltaMs: number): T {
  if (deltaMs === 0) return structuredClone(value);
  const walk = (v: unknown): unknown => {
    if (typeof v === "string") return shiftIso(v, deltaMs);
    if (Array.isArray(v)) return v.map(walk);
    if (v && typeof v === "object") {
      const out: Record<string, unknown> = {};
      for (const [k, x] of Object.entries(v)) out[k] = walk(x);
      return out;
    }
    return v;
  };
  return walk(value) as T;
}
