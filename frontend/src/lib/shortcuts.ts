/** Keyboard-shortcut sequencing (pure; unit-tested). Supports single keys and "g <key>" chords. */

export interface Shortcut {
  keys: string; // "?" | "n" | "g d"
  description: string;
}

const isTypingTarget = (t: EventTarget | null): boolean => {
  if (!t || typeof (t as HTMLElement).tagName !== "string") return false;
  const el = t as HTMLElement;
  return el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
};

export interface KeyLike {
  key: string;
  metaKey?: boolean;
  ctrlKey?: boolean;
  altKey?: boolean;
  target?: EventTarget | null;
}

/**
 * Feed key events; returns the matched shortcut ("g d", "n", ...) or null. A "g" starts a chord
 * that expires after `timeoutMs`.
 */
export function createSequencer(
  bindings: string[],
  timeoutMs = 1200,
  now: () => number = Date.now,
) {
  let pending: { key: string; at: number } | null = null;
  return (e: KeyLike): string | null => {
    if (e.metaKey || e.ctrlKey || e.altKey || isTypingTarget(e.target ?? null)) return null;
    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    if (key === "?" || e.key === "?") {
      pending = null;
      return bindings.includes("?") ? "?" : null;
    }
    if (pending && now() - pending.at <= timeoutMs) {
      const combo = `${pending.key} ${key}`;
      pending = null;
      if (bindings.includes(combo)) return combo;
    }
    pending = null;
    if (bindings.some((b) => b.startsWith(`${key} `))) {
      pending = { key, at: now() };
      return null;
    }
    return bindings.includes(key) ? key : null;
  };
}
