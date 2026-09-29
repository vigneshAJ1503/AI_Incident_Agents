"use client";

import { useEffect, useSyncExternalStore } from "react";

/** Layout density, persisted per browser. Compact tightens card padding and table rows. */
export type Density = "comfortable" | "compact";

const KEY = "aiops-density";
const listeners = new Set<() => void>();

export function readDensity(): Density {
  try {
    return localStorage.getItem(KEY) === "compact" ? "compact" : "comfortable";
  } catch {
    return "comfortable"; // storage blocked (private mode, sandboxed preview)
  }
}

export function setDensity(d: Density) {
  try {
    localStorage.setItem(KEY, d);
  } catch {
    // not persisted; still applied for this page view
  }
  document.documentElement.dataset.density = d;
  listeners.forEach((l) => l());
}

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

export function useDensity(): [Density, (d: Density) => void] {
  const density = useSyncExternalStore(subscribe, readDensity, () => "comfortable" as const);
  useEffect(() => {
    document.documentElement.dataset.density = density;
  }, [density]);
  return [density, setDensity];
}
