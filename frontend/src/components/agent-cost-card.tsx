import { CoinsIcon } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { DashboardSummary } from "@/lib/api/schemas";
import { formatCost, formatTokens } from "@/lib/format";

/** PR-041: dashboard card with tokens and estimated LLM cost per agent over the window. */
export function AgentCostCard({
  agents,
  cost,
}: {
  agents: DashboardSummary["agents"];
  cost?: DashboardSummary["cost"];
}) {
  const sorted = [...agents].sort((a, b) => b.cost_usd - a.cost_usd || b.tokens - a.tokens);
  return (
    <Card data-testid="agent-cost">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CoinsIcon aria-hidden className="size-4 text-muted-foreground" />
          Cost by agent
        </CardTitle>
        <CardDescription>
          {cost
            ? `${formatCost(cost.total_usd)} in total · ${formatCost(cost.avg_usd_per_investigation)} and ${formatTokens(cost.avg_tokens_per_investigation)} tokens per investigation`
            : "Estimated LLM cost from the profile's price table"}
        </CardDescription>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <table className="w-full text-sm">
          <caption className="sr-only">Tokens and estimated LLM cost per agent</caption>
          <thead>
            <tr className="text-left text-xs text-muted-foreground">
              <th scope="col" className="pb-2 font-medium">
                Agent
              </th>
              <th scope="col" className="pb-2 text-right font-medium">
                Tokens
              </th>
              <th scope="col" className="pb-2 text-right font-medium">
                Cost
              </th>
              <th scope="col" className="pb-2 text-right font-medium">
                Per run
              </th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {sorted.map((a) => (
              <tr key={a.name}>
                <td className="py-2">{a.name}</td>
                <td className="py-2 text-right text-xs tabular-nums">{formatTokens(a.tokens)}</td>
                <td className="py-2 text-right text-xs tabular-nums">{formatCost(a.cost_usd)}</td>
                <td className="py-2 text-right text-xs tabular-nums">
                  {formatCost(a.avg_cost_usd)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}
