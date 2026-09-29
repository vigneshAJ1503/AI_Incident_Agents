"use client";

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  ArrowUpIcon,
  ClockIcon,
  GlobeIcon,
  LoaderCircleIcon,
  ServerIcon,
  SparklesIcon,
  UserIcon,
} from "lucide-react";
import type { Route } from "next";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import type * as React from "react";
import { toast } from "sonner";

import { AnswerCard } from "@/components/ask/answer-card";
import { itemVariants, listVariants } from "@/components/motion";
import { Button } from "@/components/ui/button";
import { NativeSelect, Textarea } from "@/components/ui/input";
import { Kbd } from "@/components/ui/kbd";
import type { AskAnswer } from "@/lib/api/schemas";
import { SCENARIO_QUESTIONS, TRY_ASKING } from "@/lib/ask/examples";
import { useAsk, useServices } from "@/lib/queries";
import { cn } from "@/lib/utils";

/** The scenario questions (scenarios/S1..S5) double as suggestions. */
const SUGGESTIONS = [
  ...SCENARIO_QUESTIONS.map((s) => ({ q: s.q, hint: s.hint })),
  { q: "Something is broken", hint: "asks to clarify" },
];

interface Turn {
  id: number;
  question: string;
  /** thinking → answered | error; "starting" = an investigation was created, opening it */
  state: "thinking" | "starting" | "answered" | "error";
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

function Avatar({ who }: { who: "user" | "assistant" }) {
  return (
    <span
      aria-hidden
      className={cn(
        "grid size-7 shrink-0 place-items-center rounded-full shadow-elev-1",
        who === "user"
          ? "bg-secondary text-secondary-foreground"
          : "bg-(image:--brand-gradient) text-white",
      )}
    >
      {who === "user" ? <UserIcon className="size-3.5" /> : <SparklesIcon className="size-3.5" />}
    </span>
  );
}

function TypingIndicator({ label }: { label: string }) {
  return (
    <div
      role="status"
      data-testid="typing"
      className="inline-flex items-center gap-2.5 rounded-2xl rounded-tl-sm glass px-4 py-3 text-sm text-muted-foreground"
    >
      <span className="flex gap-1" aria-hidden>
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className="size-1.5 animate-[typing-dot_1.2s_ease-in-out_infinite] rounded-full bg-primary motion-reduce:animate-none"
            style={{ animationDelay: `${i * 0.15}s` }}
          />
        ))}
      </span>
      {label}
    </div>
  );
}

function Message({ turn, onAsk }: { turn: Turn; onAsk: (q: string) => void }) {
  return (
    <motion.li
      layout="position"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex flex-col gap-3"
    >
      <div className="flex items-end justify-end gap-2">
        <p className="max-w-[85%] rounded-2xl rounded-br-sm bg-primary px-3.5 py-2 text-sm text-primary-foreground shadow-elev-2">
          <span className="sr-only">You asked: </span>
          {turn.question}
        </p>
        <Avatar who="user" />
      </div>
      <div className="flex items-start gap-2">
        <Avatar who="assistant" />
        <div className="min-w-0 flex-1">
          <AnimatePresence mode="wait" initial={false}>
            {turn.state === "thinking" || turn.state === "starting" ? (
              <motion.div key="typing" exit={{ opacity: 0 }}>
                <TypingIndicator
                  label={
                    turn.state === "starting"
                      ? "Starting the investigation, opening the live view…"
                      : "Thinking…"
                  }
                />
              </motion.div>
            ) : turn.answer ? (
              <motion.div
                key="answer"
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
              >
                <AnswerCard answer={turn.answer} onAsk={onAsk} />
              </motion.div>
            ) : (
              <motion.p
                key="error"
                role="alert"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                className="rounded-2xl rounded-tl-sm border-danger/40 glass p-3 text-sm"
              >
                {turn.error ?? "No answer."}
              </motion.p>
            )}
          </AnimatePresence>
        </div>
      </div>
    </motion.li>
  );
}

function AskPage() {
  const router = useRouter();
  const params = useSearchParams();
  const services = useServices();
  const ask = useAsk();
  const reduce = useReducedMotion();
  const [question, setQuestion] = useState("");
  const [service, setService] = useState("");
  const [environment, setEnvironment] = useState("");
  const [since, setSince] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const ref = useRef<HTMLTextAreaElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const asked = useRef<string | null>(null);

  const update = (id: number, patch: Partial<Turn>) =>
    setTurns((ts) => ts.map((t) => (t.id === id ? { ...t, ...patch } : t)));

  const submit = async (q = question) => {
    const text = q.trim();
    if (text.length < 3 || ask.isPending) return;
    // optimistic: the question shows at once with a typing indicator
    const id = Date.now();
    setTurns((t) => [...t, { id, question: text, state: "thinking" }]);
    setQuestion("");
    try {
      const res = await ask.mutateAsync({
        question: text,
        ...(service ? { service } : {}),
        ...(environment ? { environment } : {}),
        ...(since ? { since } : {}),
      });
      if (res.investigation_id) {
        update(id, { state: "starting" });
        router.push(`/investigations/${res.investigation_id}` as Route);
        return;
      }
      update(id, { state: "answered", answer: res.answer ?? undefined });
    } catch (err) {
      const message = (err as Error).message;
      update(id, { state: "error", error: message });
      setQuestion(text); // give the question back so it can be retried
      toast.error("Could not answer", { description: message });
    }
  };

  // keep the newest message in view
  const count = turns.length;
  const lastState = turns.at(-1)?.state;
  useEffect(() => {
    if (count > 0)
      endRef.current?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "nearest" });
  }, [count, lastState, reduce]);

  // ⌘K "Ask" hands the question over as ?q=… (asked once per question)
  const fromPalette = params.get("q");
  useEffect(() => {
    if (fromPalette && asked.current !== fromPalette) {
      asked.current = fromPalette;
      void submit(fromPalette);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run when the handed-over question changes
  }, [fromPalette]);

  const quick = (q: string) => () => void submit(q);
  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void submit();
    }
  };

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-8 pt-4 md:pt-10">
      <div className="text-center">
        <motion.span
          initial={{ scale: 0.8, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          className="mx-auto mb-4 grid size-12 place-items-center rounded-2xl bg-(image:--brand-gradient) text-white shadow-elev-3"
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

      {turns.length > 0 && (
        <section aria-label="Conversation" data-testid="conversation">
          <ol className="flex flex-col gap-6" aria-live="polite">
            {turns.map((t) => (
              <Message key={t.id} turn={t} onAsk={(q) => void submit(q)} />
            ))}
          </ol>
          <div ref={endRef} />
        </section>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
        className="sticky bottom-3 z-10 rounded-2xl border border-glass-border glass-chrome p-3 shadow-elev-3 transition-shadow focus-within:ring-[3px] focus-within:ring-ring/30"
      >
        <label htmlFor="question" className="sr-only">
          Your question
        </label>
        <Textarea
          id="question"
          ref={ref}
          autoFocus
          rows={turns.length ? 2 : 3}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={
            turns.length
              ? "Ask a follow-up…"
              : "e.g. Payment API is returning HTTP 500 in production"
          }
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
            className="rounded-full"
            disabled={question.trim().length < 3 || ask.isPending}
          >
            {ask.isPending ? <LoaderCircleIcon className="animate-spin" /> : <ArrowUpIcon />}
          </Button>
        </div>
      </form>

      <div className="-mt-5 flex flex-wrap items-center gap-2 text-xs" data-testid="try-asking">
        <span className="text-muted-foreground">Quick replies</span>
        {TRY_ASKING.map((q) => (
          <button
            key={q}
            type="button"
            onClick={quick(q)}
            disabled={ask.isPending}
            className="cursor-pointer rounded-full border border-glass-border bg-glass px-2.5 py-1 shadow-elev-1 transition-[background-color,border-color,transform] hover:-translate-y-px hover:border-primary/40 hover:bg-accent active:translate-y-0 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {q}
          </button>
        ))}
      </div>

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
                onClick={quick(s.q)}
                className="group flex w-full lift cursor-pointer flex-col items-start gap-0.5 rounded-xl glass p-3 text-left text-sm"
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
