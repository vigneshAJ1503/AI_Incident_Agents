import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DiffHunk } from "@/components/evidence/evidence-body";
import s1 from "@/demo/scenario-S1.json";
import { Investigation } from "@/lib/api/schemas";

import { CommitData, LogData, MetricData, collectEvidence, linkLabel, view } from "./evidence";

const inv = Investigation.parse(s1.investigation);
const ev = collectEvidence(inv);

describe("evidence helpers", () => {
  it("collects every cited evidence id of the top hypothesis", () => {
    const top = inv.hypotheses[0]!;
    for (const id of [...top.supporting_evidence_ids, ...top.contradicting_evidence_ids]) {
      expect(ev.has(id), id).toBe(true);
    }
    expect(ev.get("ev-s1-logs-1")?.agent).toBe("logs");
  });

  it("parses kind-specific data views", () => {
    expect(view(LogData, ev.get("ev-s1-logs-1")!)?.count).toBe(44);
    const m = view(MetricData, ev.get("ev-s1-metrics-1")!);
    expect(m?.anomaly?.start).toBeTruthy();
    expect(m?.series[0]?.points.length).toBeGreaterThan(30);
    const c = view(CommitData, ev.get("ev-s1-code-1")!);
    expect(c?.files[0]?.hunk).toContain('+  DB_POOL_SIZE: "2"');
    // a mismatched view returns null instead of throwing
    expect(view(CommitData, ev.get("ev-s1-logs-1")!)).toBeNull();
  });

  it("labels deep links by target", () => {
    expect(linkLabel(ev.get("ev-s1-logs-1")!)).toBe("View in Kibana");
    expect(linkLabel(ev.get("ev-s1-metrics-1")!)).toBe("Open in Grafana");
    expect(linkLabel(ev.get("ev-s1-tickets-1")!)).toBe("Open ticket");
    expect(linkLabel(ev.get("ev-s1-knowledge-1")!)).toBe("Open runbook");
  });
});

describe("DiffHunk", () => {
  it("marks added and removed lines", () => {
    const { container } = render(
      <DiffHunk hunk={'@@ -2 +2 @@\n-  DB_POOL_SIZE: "20"\n+  DB_POOL_SIZE: "2"'} />,
    );
    expect(screen.getByLabelText("Diff")).toBeInTheDocument();
    const lines = container.querySelectorAll("pre > div");
    expect(lines).toHaveLength(3);
    expect(lines[1]?.className).toContain("bg-danger-bg");
    expect(lines[2]?.className).toContain("bg-ok-bg");
    expect(lines[2]?.textContent).toContain('DB_POOL_SIZE: "2"');
  });
});
