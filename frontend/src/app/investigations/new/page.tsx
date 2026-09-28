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
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { toast } from "sonner";

import { itemVariants, listVariants } from "@/components/motion";
import { Button } from "@/components/ui/button";
import { NativeSelect, Textarea } from "@/components/ui/input";
import { Kbd } from "@/components/ui/kbd";
import { useCreateInvestigation, useServices } from "@/lib/queries";

/** The scenario questions (scenarios/S1..S5) double as suggestions. */
const SUGGESTIONS = [
  { q: "Payment API is returning HTTP 500 in production", hint: "S1 · DB pool" },
  { q: "Orders are failing intermittently in production", hint: "S2 · OOM" },
  { q: "Why are orders timing out in production?", hint: "S3 · slow dependency" },
  { q: "Login and checkout requests are failing in production", hint: "S4 · bad deploy" },
  { q: "Payments are slow in production", hint: "S5 · cache" },
  { q: "Something is broken", hint: "asks to clarify" },
];

const RANGES = [
  { v: "15m", label: "Last 15 min" },
  { v: "30m", label: "Last 30 min" },
  { v: "1h", label: "Last hour" },
  { v: "6h", label: "Last 6 hours" },
  { v: "24h", label: "Last 24 hours" },
];

export default function NewInvestigationPage() {
  const router = useRouter();
  const services = useServices();
  const create = useCreateInvestigation();
  const [question, setQuestion] = useState("");
  const [service, setService] = useState("");
  const [environment, setEnvironment] = useState("");
  const [since, setSince] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  const submit = async (q = question) => {
    const text = q.trim();
    if (text.length < 3 || create.isPending) return;
    try {
      const res = await create.mutateAsync({
        question: text,
        ...(service ? { service } : {}),
        ...(environment ? { environment } : {}),
        ...(since ? { since } : {}),
      });
      router.push(`/investigations/${res.id}` as Route);
    } catch (err) {
      toast.error("Could not start the investigation", { description: (err as Error).message });
    }
  };

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
            aria-label="Start investigation"
            disabled={question.trim().length < 3 || create.isPending}
          >
            {create.isPending ? <LoaderCircleIcon className="animate-spin" /> : <ArrowUpIcon />}
          </Button>
        </div>
      </form>

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
