"use client";

import { AnimatePresence, motion } from "framer-motion";

import type { Evidence } from "@/lib/api/schemas";
import { formatClock } from "@/lib/format";

import { EVIDENCE_META } from "./agent-meta";

/** Evidence cards sliding in as agents collect them (newest first). */
export function EvidenceFeed({ items, max = 8 }: { items: Evidence[]; max?: number }) {
  const shown = [...items].reverse().slice(0, max);
  return (
    <div>
      <ul className="space-y-2" aria-label="Collected evidence">
        <AnimatePresence initial={false}>
          {shown.map((e) => {
            const meta = EVIDENCE_META[e.kind];
            return (
              <motion.li
                key={e.id}
                layout
                initial={{ opacity: 0, x: 24 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0 }}
                transition={{ type: "spring", stiffness: 380, damping: 32 }}
                className="flex gap-3 rounded-lg border bg-card p-3"
              >
                <span className="grid size-7 shrink-0 place-items-center rounded-md bg-muted">
                  <meta.icon aria-hidden className="size-3.5" />
                </span>
                <div className="min-w-0">
                  <p className="text-xs text-muted-foreground">
                    {meta.label} · {e.source}
                    {e.timestamp ? ` · ${formatClock(e.timestamp)}` : ""} ·{" "}
                    <span className="font-mono">{e.id}</span>
                  </p>
                  <p className="text-sm">{e.summary}</p>
                </div>
              </motion.li>
            );
          })}
        </AnimatePresence>
      </ul>
      {items.length > max && (
        <p className="mt-2 text-xs text-muted-foreground">
          +{items.length - max} more in the report
        </p>
      )}
    </div>
  );
}
