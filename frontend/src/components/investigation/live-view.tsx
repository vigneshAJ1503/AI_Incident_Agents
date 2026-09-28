"use client";

import { motion } from "framer-motion";
import { BanIcon, RadioIcon, SparklesIcon, UserIcon, WifiOffIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { StreamState } from "@/lib/api";
import { getClient } from "@/lib/api";
import { formatDuration, formatPercent } from "@/lib/format";
import { isTerminal, progress, type LiveState } from "@/lib/live/reducer";
import { cn } from "@/lib/utils";

import { AgentLane } from "./agent-lane";
import { ClarificationPrompt } from "./clarification";
import { EventLog } from "./event-log";
import { EvidenceFeed } from "./evidence-feed";

const PHASE_LABEL: Record<LiveState["phase"], string> = {
  idle: "Starting",
  connecting: "Connecting",
  clarifying: "Waiting for your answer",
  planning: "Planning the investigation",
  investigating: "Agents investigating",
  rca: "Correlating findings",
  done: "Report ready",
  failed: "Investigation failed",
  cancelled: "Investigation cancelled",
};

function useElapsed(startIso: string, running: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [running]);
  return Math.max(0, now - Date.parse(startIso));
}

export function LiveView({
  id,
  question,
  createdAt,
  state,
  stream,
  streamError,
}: {
  id: string;
  question: string;
  createdAt: string;
  state: LiveState;
  stream: StreamState;
  streamError: string | null;
}) {
  const terminal = isTerminal(state.phase);
  const elapsed = useElapsed(createdAt, !terminal);
  const { done, total, ratio } = progress(state);
  const lanes = state.order.map((k) => state.lanes[k]!).filter(Boolean);
  const [cancelling, setCancelling] = useState(false);

  const cancel = async () => {
    setCancelling(true);
    try {
      await getClient().cancel(id);
      toast("Cancelling the investigation…");
    } catch (err) {
      toast.error("Could not cancel", { description: (err as Error).message });
    } finally {
      setCancelling(false);
    }
  };

  return (
    <div className="space-y-5" data-testid="live-view" data-phase={state.phase}>
      <div className="flex justify-end">
        <div className="flex max-w-2xl items-start gap-2 rounded-2xl rounded-tr-sm bg-primary px-4 py-2.5 text-sm text-primary-foreground shadow-sm">
          <span className="sr-only">You asked:</span>
          {state.question ?? question}
          <UserIcon aria-hidden className="mt-0.5 size-4 shrink-0 opacity-70" />
        </div>
      </div>

      <Card>
        <CardContent className="space-y-3 pt-5">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <span className="flex items-center gap-2 font-medium" role="status" aria-live="polite">
              {!terminal && (
                <span className="relative flex size-2.5" aria-hidden>
                  <span className="absolute inline-flex size-full animate-ping rounded-full bg-info opacity-60 motion-reduce:hidden" />
                  <span className="relative inline-flex size-2.5 rounded-full bg-info" />
                </span>
              )}
              {PHASE_LABEL[state.phase]}
            </span>
            <span className="text-sm text-muted-foreground tabular-nums">
              {total > 0 && `${done}/${total} agents · `}
              {formatDuration(state.durationMs ?? elapsed)}
              {state.currentRound > 0 && ` · round ${state.currentRound}`}
            </span>
            <span
              className={cn(
                "ml-auto flex items-center gap-1.5 text-xs",
                stream === "reconnecting" ? "text-warn" : "text-muted-foreground",
              )}
            >
              {stream === "reconnecting" ? (
                <WifiOffIcon aria-hidden className="size-3.5" />
              ) : (
                <RadioIcon aria-hidden className="size-3.5" />
              )}
              {stream === "open" ? "live" : stream}
            </span>
            {!terminal && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => void cancel()}
                disabled={cancelling}
                data-testid="cancel"
              >
                <BanIcon /> Cancel
              </Button>
            )}
          </div>
          <div
            className="h-1.5 overflow-hidden rounded-full bg-muted"
            role="progressbar"
            aria-label="Investigation progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(ratio * 100)}
          >
            <motion.div
              className={cn(
                "h-full rounded-full",
                state.phase === "failed" ? "bg-danger" : "bg-primary",
              )}
              initial={false}
              animate={{ width: `${Math.max(4, ratio * 100)}%` }}
              transition={{ type: "spring", stiffness: 120, damping: 24 }}
            />
          </div>
          {(streamError || state.error) && (
            <p className="text-xs text-danger" role="alert">
              {state.error?.message ?? streamError}
            </p>
          )}
        </CardContent>
      </Card>

      {state.clarification && (
        <ClarificationPrompt
          id={id}
          question={state.clarification.question}
          candidates={state.clarification.candidates}
        />
      )}

      {lanes.length > 0 && <AgentLane lanes={lanes} rounds={state.rounds.map((r) => r.round)} />}

      {(state.phase === "rca" || state.hypotheses.length > 0) && (
        <Card className="relative overflow-hidden">
          {state.phase === "rca" && state.hypotheses.length === 0 && (
            <div
              aria-hidden
              className="absolute inset-0 -translate-x-full animate-[shimmer_1.6s_infinite] bg-gradient-to-r from-transparent via-primary/5 to-transparent motion-reduce:hidden"
            />
          )}
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <SparklesIcon aria-hidden className="size-4 text-primary" /> RCA agent
            </CardTitle>
            <CardDescription>
              {state.hypotheses.length
                ? "Ranked hypotheses"
                : "Correlating logs, metrics, alerts, changes and runbooks…"}
            </CardDescription>
          </CardHeader>
          {state.hypotheses.length > 0 && (
            <CardContent>
              <ol className="space-y-3">
                {state.hypotheses.map((h, i) => (
                  <motion.li
                    key={h.id}
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: i * 0.08 }}
                    className="space-y-1"
                  >
                    <div className="flex items-start justify-between gap-3 text-sm">
                      <span className={cn(i === 0 && "font-medium")}>{h.statement}</span>
                      <span className="shrink-0 tabular-nums">{formatPercent(h.confidence)}</span>
                    </div>
                    <div className="h-1 overflow-hidden rounded-full bg-muted" aria-hidden>
                      <motion.div
                        className={cn(
                          "h-full rounded-full",
                          i === 0 ? "bg-ok" : "bg-muted-foreground/40",
                        )}
                        initial={{ width: 0 }}
                        animate={{ width: `${h.confidence * 100}%` }}
                        transition={{ duration: 0.8, ease: "easeOut" }}
                      />
                    </div>
                  </motion.li>
                ))}
              </ol>
            </CardContent>
          )}
        </Card>
      )}

      <div className="grid gap-5 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <CardHeader>
            <CardTitle>Event log</CardTitle>
            <CardDescription>Streamed from the orchestrator (UTC)</CardDescription>
          </CardHeader>
          <CardContent>
            <EventLog entries={state.log} />
          </CardContent>
        </Card>
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Evidence</CardTitle>
            <CardDescription>{state.evidence.length} items collected so far</CardDescription>
          </CardHeader>
          <CardContent>
            {state.evidence.length ? (
              <EvidenceFeed items={state.evidence} />
            ) : (
              <p className="text-sm text-muted-foreground">
                Evidence appears here as agents find it.
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
