"use client";

import { CircleHelpIcon, SendIcon } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getClient } from "@/lib/api";

/** UC-13: one targeted question with candidate chips from the firing alerts. */
export function ClarificationPrompt({
  id,
  question,
  candidates,
  onAnswered,
}: {
  id: string;
  question: string;
  candidates: string[];
  onAnswered?: () => void;
}) {
  const [custom, setCustom] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const answer = async (value: string) => {
    const v = value.trim();
    if (!v) return;
    setBusy(v);
    try {
      await getClient().clarify(id, v);
      toast.success("Thanks: resuming the investigation", { description: v });
      onAnswered?.();
    } catch (err) {
      toast.error("Could not send the answer", { description: (err as Error).message });
    } finally {
      setBusy(null);
    }
  };

  return (
    <section
      aria-labelledby="clarify-title"
      className="rounded-xl border border-purple/40 bg-purple-bg/60 p-5"
      data-testid="clarification"
    >
      <div className="flex items-start gap-3">
        <CircleHelpIcon aria-hidden className="mt-0.5 size-5 shrink-0 text-purple" />
        <div className="min-w-0 flex-1 space-y-3">
          <div>
            <h2 id="clarify-title" className="font-medium">
              The planner needs one detail
            </h2>
            <p className="text-sm text-muted-foreground">{question}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            {candidates.map((c) => (
              <Button
                key={c}
                variant="outline"
                size="sm"
                disabled={busy !== null}
                onClick={() => void answer(c)}
              >
                {c}
              </Button>
            ))}
          </div>
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              void answer(custom);
            }}
          >
            <Input
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              placeholder="Or type an answer, e.g. user-service since 10:05"
              aria-label="Clarification answer"
            />
            <Button type="submit" disabled={!custom.trim() || busy !== null}>
              <SendIcon /> Send
            </Button>
          </form>
        </div>
      </div>
    </section>
  );
}
