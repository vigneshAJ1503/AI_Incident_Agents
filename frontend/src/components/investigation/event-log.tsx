"use client";

import { useEffect, useRef, useState } from "react";

import { formatClock } from "@/lib/format";
import type { LogEntry } from "@/lib/live/reducer";
import { cn } from "@/lib/utils";

const TONE: Record<LogEntry["tone"], string> = {
  info: "text-foreground",
  ok: "text-ok",
  warn: "text-warn",
  danger: "text-danger",
  muted: "text-muted-foreground",
};

/** Timestamped event log; sticks to the bottom unless the user scrolled up. */
export function EventLog({ entries }: { entries: LogEntry[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const [stick, setStick] = useState(true);

  useEffect(() => {
    const el = ref.current;
    if (el && stick) el.scrollTop = el.scrollHeight;
  }, [entries.length, stick]);

  return (
    <div
      ref={ref}
      role="log"
      aria-live="polite"
      aria-label="Investigation event log"
      tabIndex={0}
      onScroll={(e) => {
        const el = e.currentTarget;
        setStick(el.scrollHeight - el.scrollTop - el.clientHeight < 24);
      }}
      className="max-h-80 overflow-y-auto rounded-lg bg-muted/40 p-3 font-mono text-[11.5px] leading-relaxed"
    >
      <ol className="space-y-1">
        {entries.length === 0 && <li className="text-muted-foreground">Waiting for events…</li>}
        {entries.map((e) => (
          <li key={e.seq} className="grid grid-cols-[auto_1fr] gap-3">
            <time dateTime={e.timestamp} className="text-muted-foreground tabular-nums">
              {formatClock(e.timestamp)}
            </time>
            <span className={cn("break-words", TONE[e.tone])}>{e.text}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}
