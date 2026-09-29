import { CoinsIcon } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { Investigation, TokenUsage } from "@/lib/api/schemas";
import { formatCost, formatDuration, formatTokens } from "@/lib/format";

import { agentMeta } from "./agent-meta";

type Row = {
  key: string;
  label: string;
  model: string | null;
  usage: TokenUsage;
  durationMs: number | null;
  cached: number;
};

/** PR-041: tokens and estimated cost per agent, plus the planner + RCA remainder. */
export function costRows(inv: Investigation): Row[] {
  const rows: Row[] = inv.results.map((r) => ({
    key: r.task_id,
    label: agentMeta(r.agent).label,
    model: r.model ?? null,
    usage: r.usage,
    durationMs: r.duration_ms,
    cached: r.tool_calls.filter((c) => c.cached).length,
  }));
  const sum = (pick: (u: TokenUsage) => number) =>
    inv.results.reduce((total, r) => total + pick(r.usage), 0);
  const rest: TokenUsage = {
    input_tokens: Math.max(0, inv.usage.input_tokens - sum((u) => u.input_tokens)),
    output_tokens: Math.max(0, inv.usage.output_tokens - sum((u) => u.output_tokens)),
    calls: Math.max(0, inv.usage.calls - sum((u) => u.calls)),
    cost_usd: Math.max(0, (inv.usage.cost_usd ?? 0) - sum((u) => u.cost_usd ?? 0)),
  };
  if (rest.calls > 0 || rest.input_tokens + rest.output_tokens > 0) {
    rows.push({
      key: "planner-rca",
      label: "Planner + RCA",
      model: null,
      usage: rest,
      durationMs: null,
      cached: 0,
    });
  }
  return rows;
}

export function CostCard({ inv }: { inv: Investigation }) {
  const rows = costRows(inv);
  const total = inv.usage;
  return (
    <Card data-testid="cost-card">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CoinsIcon aria-hidden className="size-4 text-muted-foreground" />
          Cost &amp; tokens
        </CardTitle>
        <CardDescription>
          {formatCost(total.cost_usd ?? 0)} estimated ·{" "}
          {formatTokens(total.input_tokens + total.output_tokens)} tokens · {total.calls} LLM calls
        </CardDescription>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <table className="w-full text-sm">
          <caption className="sr-only">Tokens and estimated cost per agent</caption>
          <thead>
            <tr className="text-left text-xs text-muted-foreground">
              <th scope="col" className="pb-2 font-medium">
                Agent
              </th>
              <th scope="col" className="pb-2 text-right font-medium">
                Tokens in / out
              </th>
              <th scope="col" className="pb-2 text-right font-medium">
                Cost
              </th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {rows.map((row) => (
              <tr key={row.key}>
                <td className="py-1.5">
                  <span className="block">{row.label}</span>
                  <span className="block text-[11px] text-muted-foreground">
                    {[
                      row.model,
                      row.durationMs !== null ? formatDuration(row.durationMs) : null,
                      row.cached > 0 ? `${row.cached} cached tool calls` : null,
                    ]
                      .filter(Boolean)
                      .join(" · ") || "—"}
                  </span>
                </td>
                <td className="py-1.5 text-right text-xs tabular-nums">
                  {formatTokens(row.usage.input_tokens)} / {formatTokens(row.usage.output_tokens)}
                </td>
                <td className="py-1.5 text-right text-xs tabular-nums">
                  {formatCost(row.usage.cost_usd ?? 0)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-3 text-[11px] text-muted-foreground">
          Estimates from the profile&apos;s <code>cost.pricing</code> table; free tiers cost $0.
        </p>
      </CardContent>
    </Card>
  );
}
