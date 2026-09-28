"use client";

import { motion } from "framer-motion";
import {
  ArrowUpIcon,
  ClockIcon,
  GlobeIcon,
  LoaderCircleIcon,
  ServerIcon,
  SparklesIcon,
} from "lucide-react";
import type { Route } from "next";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { AnswerCard } from "@/components/ask/answer-card";
import { itemVariants, listVariants } from "@/components/motion";
import { Button } from "@/components/ui/button";
import { NativeSelect, Textarea } from "@/components/ui/input";
import { Kbd } from "@/components/ui/kbd";
import type { AskAnswer } from "@/lib/api/schemas";
import { SCENARIO_QUESTIONS, TRY_ASKING } from "@/lib/ask/examples";
import { useAsk, useServices } from "@/lib/queries";

/** The scenario questions (scenarios/S1..S5) double as suggestions. */
const SUGGESTIONS = [
  ...SCENARIO_QUESTIONS.map((s) => ({ q: s.q, hint: s.hint })),
  { q: "Something is broken", hint: "asks to clarify" },
];

interface Turn {
  id: number;
  question: string;
  answer?: AskAnswer;
  error?: string;
}

const RANGES = [
  { v: "15m", label: "Last 15 min" },
  { v: "30m", label: "Last 30 min" },
  { v: "1h", label: "Last hour" },
  { v: "6h", label: "Last 6 hours" },
  { v: "24h", label: "Last 24 hours" },
];

export default function NewInvestigationPage() {
  return (
    <Suspense>
      <AskPage />
    </Suspense>
  );
}

function AskPage() {
  const router = useRouter();
  const params = useSearchParams();
  const services = useServices();
  const ask = useAsk();
  const [question, setQuestion] = useState("");
  const [service, setService] = useState("");
  const [environment, setEnvironment] = useState("");
  const [since, setSince] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const ref = useRef<HTMLTextAreaElement>(null);
  const asked = useRef<string | null>(null);

  const submit = async (q = question) => {
    const text = q.trim();
    if (text.length < 3 || ask.isPending) return;
    try {
      const res = await ask.mutateAsync({
        question: text,
        ...(service ? { service } : {}),
        ...(environment ? { environment } : {}),
        ...(since ? { since } : {}),
      });
      if (res.investigation_id) {
        router.push(`/investigations/${res.investigation_id}` as Route);
        return;
      }
      setQuestion("");
      setTurns((t) => [{ id: Date.now(), question: text, answer: res.answer ?? undefined }, ...t]);
    } catch (err) {
      const message = (err as Error).message;
      setTurns((t) => [{ id: Date.now(), question: text, error: message }, ...t]);
      toast.error("Could not answer", { description: message });
    }
  };

  // ⌘K "Ask" hands the question over as ?q=… (asked once per question)
  const fromPalette = params.get("q");
  useEffect(() => {
    if (fromPalette && asked.current !== fromPalette) {
      asked.current = fromPalette;
      setQuestion(fromPalette);
      void submit(fromPalette);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run when the handed-over question changes
  }, [fromPalette]);

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-8 pt-4 md:pt-10">
      <div className="text-center">
        <motion.span
          initial={{ scale: 0.8, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          className="mx-auto mb-4 grid size-12 place-items-center rounded-2xl bg-primary text-primary-foreground shadow-md"
        >
          <SparklesIcon aria-hidden className="size-6" />
        </motion.span>
        <h1 className="text-2xl font-semibold tracking-tight md:text-3xl">
          What&apos;s going wrong?
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Ask in plain language. Seven specialist agents check logs, metrics, alerts, Kubernetes,
          code changes, tickets and runbooks, then an RCA agent explains the root cause with
          evidence.
        </p>
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
        className="rounded-2xl border bg-card p-3 shadow-sm focus-within:ring-[3px] focus-within:ring-ring/30"
      >
        <label htmlFor="question" className="sr-only">
          Your question
        </label>
        <Textarea
          id="question"
          ref={ref}
          autoFocus
          rows={3}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void submit();
            }
          }}
          placeholder="e.g. Payment API is returning HTTP 500 in production"
          className="resize-none border-0 bg-transparent text-base shadow-none focus-visible:ring-0"
        />
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <ServerIcon aria-hidden className="size-3.5" />
            <span className="sr-only">Service</span>
            <NativeSelect
              value={service}
              onChange={(e) => setService(e.target.value)}
              className="h-8 text-xs"
              aria-label="Service"
            >
              <option value="">Any service</option>
              {services.data?.map((s) => (
                <option key={s.name} value={s.name}>
                  {s.name}
                </option>
              ))}
            </NativeSelect>
          </label>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <GlobeIcon aria-hidden className="size-3.5" />
            <NativeSelect
              value={environment}
              onChange={(e) => setEnvironment(e.target.value)}
              className="h-8 text-xs"
              aria-label="Environment"
            >
              <option value="">Any environment</option>
              <option value="production">production</option>
              <option value="staging">staging</option>
            </NativeSelect>
          </label>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <ClockIcon aria-hidden className="size-3.5" />
            <NativeSelect
              value={since}
              onChange={(e) => setSince(e.target.value)}
              className="h-8 text-xs"
              aria-label="Time range"
            >
              <option value="">Auto time range</option>
              {RANGES.map((r) => (
                <option key={r.v} value={r.v}>
                  {r.label}
                </option>
              ))}
            </NativeSelect>
          </label>
          <span className="ml-auto hidden text-xs text-muted-foreground sm:inline">
            <Kbd>Enter</Kbd> to ask
          </span>
          <Button
            type="submit"
            size="icon"
            aria-label="Ask"
            disabled={question.trim().length < 3 || ask.isPending}
          >
            {ask.isPending ? <LoaderCircleIcon className="animate-spin" /> : <ArrowUpIcon />}
          </Button>
        </div>
      </form>

      <div className="-mt-5 flex flex-wrap items-center gap-2 text-xs" data-testid="try-asking">
        <span className="text-muted-foreground">Try asking…</span>
        {TRY_ASKING.map((q) => (
          <button
            key={q}
            type="button"
            onClick={() => {
              setQuestion(q);
              void submit(q);
            }}
            className="cursor-pointer rounded-full border bg-card px-2.5 py-1 transition-colors hover:border-primary/40 hover:bg-accent"
          >
            {q}
          </button>
        ))}
      </div>

      {turns.length > 0 && (
        <div className="flex flex-col gap-4" aria-live="polite" data-testid="conversation">
          {turns.map((t) => (
            <motion.div
              key={t.id}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              className="flex flex-col gap-2"
            >
              <p className="self-end rounded-2xl rounded-br-sm bg-primary px-3 py-2 text-sm text-primary-foreground">
                {t.question}
              </p>
              {t.answer && (
                <AnswerCard
                  answer={t.answer}
                  onAsk={(q) => {
                    setQuestion(q);
                    void submit(q);
                  }}
                />
              )}
              {t.error && (
                <p role="alert" className="rounded-xl border border-danger/40 p-3 text-sm">
                  {t.error}
                </p>
              )}
            </motion.div>
          ))}
        </div>
      )}

      <section aria-labelledby="suggestions">
        <h2 id="suggestions" className="mb-3 text-xs font-medium text-muted-foreground">
          Try one of the incident scenarios
        </h2>
        <motion.ul
          className="grid gap-2 sm:grid-cols-2"
          variants={listVariants}
          initial="hidden"
          animate="show"
        >
          {SUGGESTIONS.map((s) => (
            <motion.li key={s.q} variants={itemVariants}>
              <button
                type="button"
                onClick={() => {
                  setQuestion(s.q);
                  void submit(s.q);
                }}
                className="group flex w-full cursor-pointer flex-col items-start gap-0.5 rounded-xl border bg-card p-3 text-left text-sm transition-colors hover:border-primary/40 hover:bg-accent"
              >
                <span className="font-medium group-hover:text-accent-foreground">{s.q}</span>
                <span className="text-xs text-muted-foreground">{s.hint}</span>
              </button>
            </motion.li>
          ))}
        </motion.ul>
      </section>
    </div>
  );
}
