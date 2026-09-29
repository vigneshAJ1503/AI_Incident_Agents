import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ReportView } from "@/components/investigation/report-view";
import s0 from "@/demo/scenario-S0.json";
import s1 from "@/demo/scenario-S1.json";
import s2 from "@/demo/scenario-S2.json";
import { Investigation } from "@/lib/api/schemas";
import { partialOf } from "@/lib/demo/partial";
import { collectEvidence } from "@/lib/evidence";

import { relatedLabel, reportOutcome, splitTimeline } from "./report";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), prefetch: vi.fn() }),
}));

const S0 = Investigation.parse(s0.investigation);
const S1 = Investigation.parse(s1.investigation);
const PARTIAL = partialOf(Investigation.parse(s2.investigation), undefined);
const kindOf = (inv: Investigation) => {
  const ev = collectEvidence(inv);
  return (id: string) => ev.get(id)?.kind;
};

describe("report outcome (live-demo regression: partial + High shown as 'No incident')", () => {
  it("names the root cause, a true healthy result, and everything in between", () => {
    expect(reportOutcome(S1)?.kind).toBe("root_cause");
    expect(reportOutcome(S0)?.kind).toBe("healthy");
    const partial = reportOutcome(PARTIAL);
    expect(partial?.kind).toBe("no_root_cause");
    if (partial?.kind !== "no_root_cause") return;
    expect(partial.failedAgents).toEqual(["metrics", "k8s"]);
    expect(partial.lead?.statement).toMatch(/Memory leak in order-service/);
    expect(partial.headline).toMatch(/no root cause identified/);
    expect(partial.headline).not.toMatch(/Weak lead/);
  });

  it("never calls abnormal signals, a partial run or a ranked lead healthy", () => {
    const base = { ...S0, report: { ...S0.report!, severity: "none" as const } };
    // the live case: partial, severity High, no hypothesis at all
    const live = {
      ...base,
      status: "partial" as const,
      report: { ...base.report, severity: "high" as const },
    };
    expect(reportOutcome(live)?.kind).toBe("no_root_cause");
    expect(reportOutcome({ ...base, status: "partial" })?.kind).toBe("no_root_cause");
    expect(reportOutcome({ ...base, hypotheses: PARTIAL.hypotheses })?.kind).toBe("no_root_cause");
    expect(reportOutcome({ ...base, report: null })).toBeNull();
  });
});

describe("timeline ordering (live-demo regression: old OPS tickets on top)", () => {
  it("puts the incident's chain first in time order and the tickets apart", () => {
    // the partial demo case stores its old tickets first, like a pre-fix backend did
    expect(PARTIAL.timeline[0]?.source).toBe("tickets:jira");
    const { chain, related } = splitTimeline(PARTIAL.timeline, kindOf(PARTIAL));
    expect(chain[0]?.description).toMatch(/v2\.3\.0 rolled out/);
    expect(chain.every((t) => !t.source.startsWith("tickets"))).toBe(true);
    const stamps = chain.map((t) => Date.parse(t.timestamp));
    expect(stamps).toEqual([...stamps].sort((a, b) => a - b));
    expect(related.map((t) => t.description.split(" ")[0])).toEqual([
      "OPS-17",
      "OPS-18",
      "OPS-19",
      "OPS-20",
    ]);
    expect(relatedLabel(related, kindOf(PARTIAL))).toBe("Related tickets (4)");
  });

  it("keeps the backend's order at equal times and recognises context without evidence", () => {
    const at = "2026-09-29T10:00:00Z";
    const { chain, related } = splitTimeline(
      [
        { timestamp: at, description: "change", source: "code:git", evidence_id: null },
        { timestamp: at, description: "rollout", source: "k8s:events", evidence_id: null },
        { timestamp: "2026-09-28T10:00:00Z", description: "rb", source: "knowledge:docs" },
      ],
      () => undefined,
    );
    expect(chain.map((t) => t.description)).toEqual(["change", "rollout"]);
    expect(relatedLabel(related, () => undefined)).toBe("Related runbooks (1)");
  });
});

function renderReport(inv: Investigation) {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <ReportView inv={inv} />
    </QueryClientProvider>,
  );
}

describe("report view", () => {
  it("heads a partial run with signals 'No root cause identified' and labels the lead", () => {
    renderReport(PARTIAL);
    const hero = screen.getByTestId("root-cause");
    expect(hero).toHaveAttribute("data-outcome", "no_root_cause");
    expect(hero).toHaveTextContent("No root cause identified");
    expect(hero).not.toHaveTextContent("No incident detected");
    expect(within(hero).getByTestId("weak-lead")).toHaveTextContent(/Weak lead · not confirmed/);
    expect(within(hero).getByTestId("no-root-cause-hint")).toHaveTextContent(
      /agents failed \(Metrics agent, Kubernetes agent\).*re-run/,
    );
    expect(screen.queryByTestId("confidence")).toBeNull();
  });

  it("groups related tickets under one collapsed row after the chain", () => {
    renderReport(PARTIAL);
    const rows = within(screen.getByTestId("timeline")).getAllByRole("button");
    expect(rows[0]).toHaveTextContent(/v2\.3\.0 rolled out/);
    expect(screen.getByTestId("timeline")).not.toHaveTextContent("OPS-17");
    const toggle = screen.getByRole("button", { name: /Related tickets \(4\)/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("timeline-related")).toHaveTextContent("OPS-17");
  });

  it("keeps S0 as a true 'No incident detected'", () => {
    renderReport(S0);
    expect(screen.getByTestId("root-cause")).toHaveTextContent("No incident detected");
    expect(screen.queryByTestId("weak-lead")).toBeNull();
  });
});
