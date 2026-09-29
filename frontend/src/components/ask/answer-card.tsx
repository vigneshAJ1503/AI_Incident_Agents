"use client";

import {
  ArrowRightIcon,
  CircleCheckIcon,
  CircleMinusIcon,
  CircleXIcon,
  InfoIcon,
  PlayIcon,
  SparklesIcon,
} from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { Fragment, type ReactNode } from "react";

import { agentMeta } from "@/components/investigation/agent-meta";
import { SeverityBadge, StatusBadge } from "@/components/status";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { AskAnswer, AskItem } from "@/lib/api/schemas";
import { formatDuration, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";

/** `**bold**` and `` `code` `` inside a line (the API's markdown is deliberately simple). */
function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**"))
      return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`"))
      return (
        <code key={i} className="rounded bg-muted px-1 font-mono text-xs">
          {part.slice(1, -1)}
        </code>
      );
    return <Fragment key={i}>{part}</Fragment>;
  });
}

/** Paragraphs and `- ` bullet lists; everything is rendered as text (no HTML). */
export function MarkdownLite({ text }: { text: string }) {
  const blocks = text.split(/\n{2,}/).filter((b) => b.trim());
  return (
    <div className="space-y-2 text-sm leading-relaxed">
      {blocks.map((block, i) => {
        const lines = block.split("\n");
        if (lines.every((l) => l.startsWith("- ")))
          return (
            <ul key={i} className="list-disc space-y-1 pl-5">
              {lines.map((l, j) => (
                <li key={j}>{inline(l.slice(2))}</li>
              ))}
            </ul>
          );
        return <p key={i}>{inline(lines.join(" "))}</p>;
      })}
    </div>
  );
}

const CHECK_ICON = {
  ok: { icon: CircleCheckIcon, className: "text-ok", label: "OK" },
  down: { icon: CircleXIcon, className: "text-danger", label: "Down" },
  disabled: { icon: CircleMinusIcon, className: "text-muted-foreground", label: "Disabled" },
  info: { icon: InfoIcon, className: "text-info", label: "Info" },
} as const;

function StatusDot({ status }: { status: string }) {
  const running = status === "running";
  return (
    <span className="relative flex size-2.5" aria-hidden>
      {running && (
        <span className="absolute inline-flex size-full animate-ping rounded-full bg-info opacity-60 motion-reduce:animate-none" />
      )}
      <span
        className={cn(
          "relative inline-flex size-2.5 rounded-full",
          running
            ? "bg-info"
            : status === "done"
              ? "bg-ok"
              : status === "failed"
                ? "bg-danger"
                : "bg-muted-foreground/40",
        )}
      />
    </span>
  );
}

function Item({ item, onAsk }: { item: AskItem; onAsk?: (q: string) => void }) {
  switch (item.type) {
    case "running_investigation":
      return (
        <li
          className="rounded-lg border border-glass-border bg-card/50 p-3"
          data-testid="running-item"
        >
          <div className="flex flex-wrap items-center gap-2">
            <StatusDot status="running" />
            <span className="font-medium">{item.question}</span>
            {item.service && <Badge tone="outline">{item.service}</Badge>}
            <span className="text-xs text-muted-foreground">
              round {item.round ?? "-"} · {formatDuration(item.elapsed_s * 1000)}
            </span>
            <Button asChild size="sm" variant="outline" className="ml-auto">
              <Link href={item.href as Route}>
                Open live view <ArrowRightIcon aria-hidden />
              </Link>
            </Button>
          </div>
          {item.agents.length > 0 && (
            <ul className="mt-2 grid gap-1 sm:grid-cols-2" aria-label="Agents">
              {item.agents.map((a) => (
                <li key={`${a.agent}-${a.round}`} className="flex items-center gap-2 text-xs">
                  <StatusDot status={a.status} />
                  <span className="font-medium">{agentMeta(a.agent).label}</span>
                  <span className="text-muted-foreground">{a.status}</span>
                  {a.tool && (
                    <code className="truncate font-mono text-[11px] text-muted-foreground">
                      {a.tool}
                    </code>
                  )}
                </li>
              ))}
            </ul>
          )}
        </li>
      );
    case "investigation":
      return (
        <li className="flex flex-wrap items-center gap-2 rounded-lg border border-glass-border bg-card/50 p-3">
          <Link href={item.href as Route} className="font-medium hover:underline">
            {item.title}
          </Link>
          <StatusBadge status={item.status} />
          {item.severity && <SeverityBadge severity={item.severity} />}
          {item.root_cause && (
            <p className="w-full text-xs text-muted-foreground">
              {item.root_cause}
              {item.confidence ? ` · ${formatPercent(item.confidence)} confidence` : ""}
            </p>
          )}
        </li>
      );
    case "agent": {
      const meta = agentMeta(item.name);
      const Icon = meta.icon;
      return (
        <li
          className="flex items-start gap-3 rounded-lg border border-glass-border bg-card/50 p-3"
          data-testid="agent-item"
        >
          <Icon aria-hidden className="mt-0.5 size-4 text-primary" />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{meta.label}</span>
              {item.providers.map((p) => (
                <Badge key={p} tone="outline">
                  {p}
                </Badge>
              ))}
              {!item.enabled && <Badge tone="neutral">disabled</Badge>}
            </div>
            <p className="text-xs text-muted-foreground">{item.description}</p>
          </div>
          <div className="text-right text-xs text-muted-foreground tabular-nums">
            <div>{formatPercent(item.success_rate_7d)} success</div>
            <div>p50 {formatDuration(item.p50_ms)}</div>
          </div>
        </li>
      );
    }
    case "check": {
      const meta = CHECK_ICON[item.status];
      const Icon = meta.icon;
      return (
        <li className="flex items-center gap-2 text-sm">
          <Icon aria-label={meta.label} className={cn("size-4", meta.className)} />
          <span className="font-medium">{item.name}</span>
          <span className="truncate text-muted-foreground">{item.detail}</span>
        </li>
      );
    }
    case "suggestion":
      return (
        <li>
          <button
            type="button"
            onClick={() => onAsk?.(item.question)}
            className="flex cursor-pointer items-center gap-2 rounded-full border border-glass-border bg-card/70 px-3 py-1.5 text-left text-sm shadow-elev-1 transition-[background-color,border-color,transform] hover:-translate-y-px hover:border-primary/40 hover:bg-accent active:translate-y-0"
            data-testid="suggestion-chip"
          >
            {item.scenario ? (
              <PlayIcon aria-hidden className="size-3.5 text-primary" />
            ) : (
              <SparklesIcon aria-hidden className="size-3.5 text-primary" />
            )}
            <span>{item.question}</span>
            {item.hint && <span className="text-xs text-muted-foreground">{item.hint}</span>}
          </button>
        </li>
      );
  }
}

/** A chat reply to a platform question (or the "no recorded scenario" answer). */
export function AnswerCard({
  answer,
  onAsk,
}: {
  answer: AskAnswer;
  onAsk?: (question: string) => void;
}) {
  const chips = answer.items.every((i) => i.type === "suggestion");
  const checks = answer.items.every((i) => i.type === "check");
  return (
    <section
      aria-label={answer.title}
      data-testid="answer-card"
      className="rounded-2xl rounded-tl-sm glass p-4"
    >
      <h2 className="mb-2 flex items-center gap-2 text-base font-semibold">
        <SparklesIcon aria-hidden className="size-4 text-primary" />
        {answer.title}
      </h2>
      <MarkdownLite text={answer.markdown} />
      {answer.items.length > 0 && (
        <ul
          className={cn(
            "mt-3",
            chips ? "flex flex-wrap gap-2" : checks ? "grid gap-1.5 sm:grid-cols-2" : "grid gap-2",
          )}
        >
          {answer.items.map((item, i) => (
            <Item key={i} item={item} onAsk={onAsk} />
          ))}
        </ul>
      )}
      {answer.links.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-3 text-sm">
          {answer.links.map((l) => (
            <Link
              key={l.href}
              href={l.href as Route}
              className="inline-flex items-center gap-1 text-primary hover:underline"
            >
              {l.label} <ArrowRightIcon aria-hidden className="size-3.5" />
            </Link>
          ))}
        </div>
      )}
    </section>
  );
}
