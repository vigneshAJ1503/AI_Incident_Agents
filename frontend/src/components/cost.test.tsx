import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DashboardSummary, Investigation } from "@/lib/api/schemas";
import { formatCost, formatTokens } from "@/lib/format";

import { AgentCostCard } from "./agent-cost-card";
import { CostCard, costRows } from "./investigation/cost-card";

const usage = (input: number, output: number, calls: number, cost?: number) => ({
  input_tokens: input,
  output_tokens: output,
  calls,
  ...(cost === undefined ? {} : { cost_usd: cost }),
});

const result = (agent: string, u: ReturnType<typeof usage>, cached = 0) => ({
  task_id: `step-${agent}`,
  agent,
  status: "success",
  summary: `${agent} done`,
  model: "llama-3.3-70b-versatile",
  duration_ms: 2400,
  usage: u,
  tool_calls: Array.from({ length: cached + 1 }, (_, i) => ({
    id: `tc-${agent}-${i}`,
    agent,
    capability: agent,
    tool: "query",
    cached: i < cached,
  })),
});

function investigation(extra: Record<string, unknown> = {}) {
  return Investigation.parse({
    id: "inv-1",
    incident: { id: "INC-1", title: "Payment API 500s", created_at: "2026-09-29T10:00:00Z" },
    status: "completed",
    created_at: "2026-09-29T10:00:00Z",
    results: [
      result("logs", usage(1200, 300, 2, 0.0021), 1),
      result("metrics", usage(800, 200, 1, 0.0009)),
    ],
    usage: usage(2600, 700, 5, 0.004),
    ...extra,
  });
}

describe("cost formatting (PR-041)", () => {
  it.each([
    [0, "$0"],
    [0.00004, "<$0.0001"],
    [0.0042, "$0.0042"],
    [1.25, "$1.25"],
  ])("%d → %s", (usd, expected) => expect(formatCost(usd)).toBe(expected));

  it("formats tokens compactly and handles missing input", () => {
    expect(formatTokens(950)).toBe("950");
    expect(formatTokens(1_250)).toBe("1.3k");
    expect(formatTokens(12_345)).toBe("12k");
    expect(formatTokens(2_500_000)).toBe("2.5M");
    expect(formatCost(undefined)).toBe("—");
  });
});

describe("CostCard", () => {
  it("lists every agent plus the planner + RCA remainder", () => {
    const rows = costRows(investigation());
    expect(rows.map((r) => r.label)).toHaveLength(3);
    const rest = rows[2]!;
    expect(rest.label).toBe("Planner + RCA");
    expect(rest.usage).toEqual({
      input_tokens: 600,
      output_tokens: 200,
      calls: 2,
      cost_usd: 0.001,
    });
    expect(rows[0]!.cached).toBe(1);
  });

  it("renders totals and per-agent cost", () => {
    render(<CostCard inv={investigation()} />);
    const card = screen.getByTestId("cost-card");
    expect(within(card).getByText(/\$0\.0040 estimated/)).toBeInTheDocument();
    expect(within(card).getByText("$0.0021")).toBeInTheDocument();
    expect(within(card).getByText(/1 cached tool calls/)).toBeInTheDocument();
  });

  it("works with payloads from before PR-041 (no cost_usd, no cached)", () => {
    const old = investigation({
      results: [result("logs", usage(0, 0, 1))],
      usage: usage(0, 0, 1),
    });
    expect(old.usage.cost_usd).toBeUndefined();
    render(<CostCard inv={old} />);
    expect(screen.getByText(/\$0 estimated/)).toBeInTheDocument();
    expect(costRows(old)).toHaveLength(1);
  });
});

describe("AgentCostCard", () => {
  it("sorts agents by cost and shows the window totals", () => {
    const agents = DashboardSummary.shape.agents.parse([
      { name: "logs", runs: 4, success_rate: 1, p50_ms: 900, tokens: 4000, cost_usd: 0.002 },
      {
        name: "metrics",
        runs: 4,
        success_rate: 1,
        p50_ms: 900,
        tokens: 9000,
        cost_usd: 0.009,
        avg_cost_usd: 0.00225,
      },
      { name: "tickets", runs: 4, success_rate: 1, p50_ms: 900 },
    ]);
    render(
      <AgentCostCard
        agents={agents}
        cost={{
          total_usd: 0.011,
          avg_usd_per_investigation: 0.0028,
          tokens: 13000,
          avg_tokens_per_investigation: 3250,
        }}
      />,
    );
    const rows = within(screen.getByTestId("agent-cost")).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell")[0]!.textContent)).toEqual([
      "metrics",
      "logs",
      "tickets",
    ]);
    expect(screen.getByText(/\$0\.0110 in total/)).toBeInTheDocument();
  });
});
