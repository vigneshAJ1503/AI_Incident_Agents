"use client";

import { ArrowDownIcon, PauseIcon, PlayIcon } from "lucide-react";
import { memo, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
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

const Entry = memo(function Entry({ e }: { e: LogEntry }) {
  return (
    <li className="grid grid-cols-[auto_1fr] gap-3">
      <time dateTime={e.timestamp} className="text-muted-foreground tabular-nums">
        {formatClock(e.timestamp)}
      </time>
      <span className={cn("break-words", TONE[e.tone])}>{e.text}</span>
    </li>
  );
});

/**
 * Timestamped event log. Follows new events (auto-scroll) until paused, either with the toggle
 * or by scrolling up; "N new" jumps back to the latest and resumes following.
 */
export function EventLog({ entries }: { entries: LogEntry[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const [paused, setPaused] = useState(false);
  const [seenCount, setSeenCount] = useState(entries.length);
  const unseen = paused ? Math.max(0, entries.length - seenCount) : 0;

  useEffect(() => {
    const el = ref.current;
    if (el && !paused) el.scrollTop = el.scrollHeight;
  }, [entries.length, paused]);

  const pause = () => {
    setSeenCount(entries.length);
    setPaused(true);
  };
  const follow = () => setPaused(false);

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          aria-pressed={paused}
          data-testid="log-pause"
          onClick={() => (paused ? follow() : pause())}
        >
          {paused ? <PlayIcon aria-hidden /> : <PauseIcon aria-hidden />}
          {paused ? "Resume auto-scroll" : "Pause auto-scroll"}
        </Button>
        {unseen > 0 && (
          <Button variant="ghost" size="sm" onClick={follow} data-testid="log-unseen">
            <ArrowDownIcon aria-hidden /> {unseen} new
          </Button>
        )}
        <span className="ml-auto text-xs text-muted-foreground tabular-nums">
          {entries.length} events
        </span>
      </div>
      <div
        ref={ref}
        role="log"
        aria-live={paused ? "off" : "polite"}
        aria-label="Investigation event log"
        tabIndex={0}
        onWheel={(e) => {
          if (e.deltaY < 0 && !paused) pause();
        }}
        onScroll={(e) => {
          const el = e.currentTarget;
          const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
          if (atBottom && paused && unseen > 0) follow();
        }}
        className="max-h-80 overflow-y-auto rounded-lg border border-glass-border bg-muted/40 p-3 font-mono text-[11.5px] leading-relaxed"
      >
        <ol className="space-y-1">
          {entries.length === 0 && <li className="text-muted-foreground">Waiting for events…</li>}
          {entries.map((e) => (
            <Entry key={e.seq} e={e} />
          ))}
        </ol>
      </div>
    </div>
  );
}
