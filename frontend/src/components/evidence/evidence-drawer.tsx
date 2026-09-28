"use client";

import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type * as React from "react";

import { EVIDENCE_META, agentMeta } from "@/components/investigation/agent-meta";
import { Badge, type Tone } from "@/components/ui/badge";
import { Dialog, DialogDescription, DialogTitle, SheetContent } from "@/components/ui/dialog";
import type { ClaimKind, Evidence } from "@/lib/api/schemas";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

import { DeepLink, EvidenceBody } from "./evidence-body";

type Ev = Evidence & { agent: string };

interface Ctx {
  evidence: Map<string, Ev>;
  open: (id: string) => void;
  highlighted: string | null;
  setHighlighted: (id: string | null) => void;
}

const EvidenceContext = createContext<Ctx | null>(null);

export function useEvidence(): Ctx {
  const ctx = useContext(EvidenceContext);
  if (!ctx) throw new Error("useEvidence must be used inside <EvidenceProvider>");
  return ctx;
}

/** Holds the investigation's evidence and the drawer that shows one item with its deep link. */
export function EvidenceProvider({
  evidence,
  children,
}: {
  evidence: Map<string, Ev>;
  children: React.ReactNode;
}) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const open = useCallback((id: string) => {
    setOpenId(id);
    setHighlighted(id);
  }, []);
  const value = useMemo(
    () => ({ evidence, open, highlighted, setHighlighted }),
    [evidence, open, highlighted],
  );
  const current = openId ? evidence.get(openId) : undefined;

  return (
    <EvidenceContext.Provider value={value}>
      {children}
      <Dialog open={openId !== null} onOpenChange={(o) => !o && setOpenId(null)}>
        <SheetContent className="sm:max-w-xl" data-testid="evidence-drawer">
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
              <EvidenceBody e={current} />
              {current.query && (
                <div className="space-y-1">
                  <p className="text-xs font-medium text-muted-foreground">Query</p>
                  <pre className="overflow-x-auto rounded-md bg-muted/50 p-2.5 font-mono text-[11.5px] whitespace-pre-wrap">
                    {current.query}
                  </pre>
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

/** Clickable citation chip: [ev-s1-logs-1] → opens the drawer. */
export function Citation({ id, tone = "default" }: { id: string; tone?: "default" | "contra" }) {
  const { open, evidence, highlighted } = useEvidence();
  const e = evidence.get(id);
  const Icon = e ? EVIDENCE_META[e.kind].icon : null;
  return (
    <button
      type="button"
      onClick={() => open(id)}
      data-testid="citation"
      title={e?.summary ?? id}
      className={cn(
        "inline-flex cursor-pointer items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono text-[11px] transition-colors hover:bg-accent",
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
