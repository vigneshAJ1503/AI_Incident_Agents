/** Deterministic helpers for the demo generator (seeded PRNG, time math). */

export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export const SEC = 1000;
export const MIN = 60 * SEC;
export const HOUR = 60 * MIN;
export const DAY = 24 * HOUR;

export const iso = (ms: number): string => new Date(ms).toISOString().replace(".000Z", "Z");
export const at = (s: string): number => Date.parse(s);

export function round(n: number, digits = 3): number {
  const f = 10 ** digits;
  return Math.round(n * f) / f;
}

export interface Point {
  t: string;
  v: number;
}

/** Sample `fn` every `stepSec` over [start, end] with multiplicative noise from `rand`. */
export function series(
  rand: () => number,
  start: number,
  end: number,
  stepSec: number,
  fn: (t: number) => number,
  noise = 0.08,
  digits = 3,
): Point[] {
  const out: Point[] = [];
  for (let t = start; t <= end; t += stepSec * SEC) {
    const base = fn(t);
    const jitter = 1 + (rand() * 2 - 1) * noise;
    out.push({ t: iso(t), v: round(Math.max(0, base * jitter), digits) });
  }
  return out;
}

/** Smooth 0→1 ramp between a and b. */
export function ramp(t: number, a: number, b: number): number {
  if (t <= a) return 0;
  if (t >= b) return 1;
  const x = (t - a) / (b - a);
  return x * x * (3 - 2 * x);
}
