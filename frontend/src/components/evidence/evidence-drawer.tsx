"use client";

import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type * as React from "react";

import { EVIDENCE_META, agentMeta } from "@/components/investigation/agent-meta";
import { Badge, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogDescription, DialogTitle, SheetContent } from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/kbd";
import type { ClaimKind, Evidence } from "@/lib/api/schemas";
import { stepTrail } from "@/lib/evidence";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

import { CodeBlock, DeepLink, EvidenceBody } from "./evidence-body";

type Ev = Evidence & { agent: string };

interface Ctx {
  evidence: Map<string, Ev>;
  /** Open one item; `trail` is the list ←/→ walks (default: all evidence). */
  open: (id: string, trail?: readonly string[]) => void;
  highlighted: string | null;
  setHighlighted: (id: string | null) => void;
}

const EvidenceContext = createContext<Ctx | null>(null);

export function useEvidence(): Ctx {
  const ctx = useContext(EvidenceContext);
  if (!ctx) throw new Error("useEvidence must be used inside <EvidenceProvider>");
  return ctx;
}

/**
 * Holds the investigation's evidence and the drawer that shows one item with its deep link.
 * ←/→ (or the buttons) move between the citations of the group the item was opened from.
 */
export function EvidenceProvider({
  evidence,
  children,
}: {
  evidence: Map<string, Ev>;
  children: React.ReactNode;
}) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [trail, setTrail] = useState<readonly string[]>([]);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const all = useMemo(() => [...evidence.keys()], [evidence]);
  const open = useCallback(
    (id: string, t?: readonly string[]) => {
      const list = (t && t.length > 1 ? t : all).filter((x) => evidence.has(x));
      setTrail(list.includes(id) ? list : all);
      setOpenId(id);
      setHighlighted(id);
    },
    [all, evidence],
  );
  const value = useMemo(
    () => ({ evidence, open, highlighted, setHighlighted }),
    [evidence, open, highlighted],
  );
  const current = openId ? evidence.get(openId) : undefined;
  const pos = openId ? trail.indexOf(openId) : -1;
  const go = (delta: 1 | -1) => {
    if (!openId) return;
    const next = stepTrail(trail, openId, delta);
    if (next) {
      setOpenId(next);
      setHighlighted(next);
    }
  };
  const onKeyDown = (e: React.KeyboardEvent) => {
    const t = e.target as HTMLElement;
    if (t.closest("input, textarea, select, [contenteditable=true]")) return;
    if (e.key === "ArrowRight") {
      e.preventDefault();
      go(1);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      go(-1);
    }
  };

  return (
    <EvidenceContext.Provider value={value}>
      {children}
      <Dialog open={openId !== null} onOpenChange={(o) => !o && setOpenId(null)}>
        <SheetContent className="sm:max-w-xl" data-testid="evidence-drawer" onKeyDown={onKeyDown}>
          {current ? (
            <>
              <div className="space-y-1.5 pr-8">
                <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <Badge tone="info">{EVIDENCE_META[current.kind].label}</Badge>
                  <span>{agentMeta(current.agent).label}</span>·<span>{current.source}</span>·
                  <span className="font-mono">{current.id}</span>
                </p>
                <DialogTitle className="text-base leading-snug">{current.summary}</DialogTitle>
                <DialogDescription>
                  {current.timestamp ? formatDateTime(current.timestamp) : "No timestamp"}
                </DialogDescription>
              </div>
              {trail.length > 1 && (
                <nav
                  aria-label="Evidence navigation"
                  className="flex items-center gap-2 rounded-lg border border-glass-border bg-glass px-2 py-1 text-xs text-muted-foreground"
                >
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label="Previous evidence"
                    data-testid="evidence-prev"
                    onClick={() => go(-1)}
                  >
                    <ChevronLeftIcon />
                  </Button>
                  <span className="tabular-nums" aria-live="polite" data-testid="evidence-pos">
                    {pos + 1} of {trail.length}
                  </span>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label="Next evidence"
                    data-testid="evidence-next"
                    onClick={() => go(1)}
                  >
                    <ChevronRightIcon />
                  </Button>
                  <span className="ml-auto hidden items-center gap-1 sm:flex">
                    <Kbd>←</Kbd>
                    <Kbd>→</Kbd> to move
                  </span>
                </nav>
              )}
              <EvidenceBody e={current} />
              {current.query && (
                <div className="space-y-1">
                  <p className="text-xs font-medium text-muted-foreground">Query</p>
                  <CodeBlock code={current.query} label="Copy query" what="Query copied" />
                </div>
              )}
              <div className="flex flex-wrap gap-2">
                <DeepLink e={current} size="default" />
              </div>
            </>
          ) : (
            <>
              <DialogTitle>Evidence not found</DialogTitle>
              <DialogDescription>{openId} is not part of this investigation.</DialogDescription>
            </>
          )}
        </SheetContent>
      </Dialog>
    </EvidenceContext.Provider>
  );
}

/** Clickable citation chip: [ev-s1-logs-1] → opens the drawer (←/→ then walks `trail`). */
export function Citation({
  id,
  tone = "default",
  trail,
}: {
  id: string;
  tone?: "default" | "contra";
  trail?: readonly string[];
}) {
  const { open, evidence, highlighted } = useEvidence();
  const e = evidence.get(id);
  const Icon = e ? EVIDENCE_META[e.kind].icon : null;
  return (
    <button
      type="button"
      onClick={() => open(id, trail)}
      data-testid="citation"
      title={e?.summary ?? id}
      className={cn(
        "inline-flex cursor-pointer items-center gap-1 rounded-md border border-glass-border bg-card/60 px-1.5 py-0.5 font-mono text-[11px] transition-[background-color,transform,box-shadow] hover:-translate-y-px hover:bg-accent hover:shadow-elev-1 active:translate-y-0",
        tone === "contra" && "border-danger/40 text-danger",
        highlighted === id && "ring-2 ring-ring",
      )}
    >
      {Icon && <Icon aria-hidden className="size-3" />}
      {id}
    </button>
  );
}

const CLAIM_TONE: Record<ClaimKind, Tone> = {
  FACT: "ok",
  OBSERVATION: "info",
  CORRELATION: "purple",
  HYPOTHESIS: "warn",
  RECOMMENDATION: "neutral",
};

export function ClaimBadge({ kind }: { kind: ClaimKind }) {
  return (
    <Badge
      tone={CLAIM_TONE[kind]}
      className="font-mono text-[10px] tracking-wide"
      title={`Claim type: ${kind}`}
    >
      {kind}
    </Badge>
  );
}
