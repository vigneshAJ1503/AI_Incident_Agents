import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DemoClient } from "@/lib/api/demo-client";
import { parseOrThrow } from "@/lib/api/parse";
import { AskResponse } from "@/lib/api/schemas";
import { classify } from "@/lib/ask/intent";

import { AnswerCard } from "./answer-card";

/** A /ask response as the API sends it (backend/tests/unit/test_api_ask.py). */
const RUNNING = {
  kind: "platform",
  intent: "agents_running",
  confidence: 0.9,
  source: "rules",
  answer: {
    title: "1 investigation running",
    markdown: "- **Payment API is returning HTTP 500** (payment-service): agents working: logs",
    items: [
      {
        type: "running_investigation",
        id: "inv-1",
        question: "Payment API is returning HTTP 500",
        service: "payment-service",
        status: "running",
        mode: "replay",
        created_at: "2026-09-29T10:00:00Z",
        elapsed_s: 12.3,
        round: 1,
        agents: [
          { agent: "logs", round: 1, status: "running", tool: "search_logs" },
          { agent: "metrics", round: 1, status: "done", tool: null },
        ],
        href: "/investigations/inv-1",
      },
    ],
    links: [{ label: "Open live view", href: "/investigations/inv-1" }],
  },
  investigation_id: null,
  mode: null,
  scenario: null,
};

describe("ask answers", () => {
  it("parse the API's /ask response with typed items", () => {
    const res = parseOrThrow(AskResponse, RUNNING, "POST /ask");
    expect(res.answer?.items[0]?.type).toBe("running_investigation");
    expect(() =>
      parseOrThrow(
        AskResponse,
        { ...RUNNING, answer: { title: "x", items: [{ type: "nope" }] } },
        "x",
      ),
    ).toThrow();
  });

  it("render a running investigation with its agents, tool and live-view link", () => {
    const res = parseOrThrow(AskResponse, RUNNING, "POST /ask");
    render(<AnswerCard answer={res.answer!} />);
    expect(screen.getByRole("heading", { name: /1 investigation running/ })).toBeInTheDocument();
    expect(screen.getByText("search_logs")).toBeInTheDocument();
    expect(screen.getByText("Logs agent")).toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: /Open live view/ });
    expect(links[0]).toHaveAttribute("href", "/investigations/inv-1");
    expect(screen.getByText("payment-service", { selector: "strong ~ *, span" })).toBeTruthy();
  });

  it("render scenario chips that ask again", async () => {
    const onAsk = vi.fn();
    render(
      <AnswerCard
        answer={{
          title: "No recorded incident matches this question",
          markdown: "In replay mode I can investigate:",
          items: [
            { type: "suggestion", question: "Payments are slow", hint: "S5", scenario: "S5" },
          ],
          links: [],
        }}
        onAsk={onAsk}
      />,
    );
    await userEvent.click(screen.getByTestId("suggestion-chip"));
    expect(onAsk).toHaveBeenCalledWith("Payments are slow");
  });
});

describe("demo classifier (mirrors backend intent.py)", () => {
  it.each([
    ["what are the agents that are running now?", "agents_running"],
    ["is the payments agent running?", "agents_running"],
    ["which agents do you have?", "agents_catalog"],
    ["is the LLM configured?", "health"],
    ["what's down?", "health"],
    ["what happened with the last payment incident?", "recent_investigations"],
    ["help", "help"],
    ["agents are failing on payment-service", "incident"],
    ["is payment-service down?", "incident"],
    ["Payment API is returning HTTP 500 in production", "incident"],
  ])("%s → %s", (q, intent) => {
    expect(classify(q).intent).toBe(intent);
  });
});

describe("DemoClient.ask", () => {
  const client = () => new DemoClient({ speed: 1, latency: 0, storage: null });

  it("answers platform questions from the dataset without creating an investigation", async () => {
    const c = client();
    const before = (await c.listInvestigations({ limit: 500 })).items.length;
    const agents = await c.ask({ question: "Which agents do you have?" });
    expect(agents.kind).toBe("platform");
    expect(agents.answer?.items).toHaveLength(7);
    const idle = await c.ask({ question: "What agents are running now?" });
    expect(idle.answer?.title).toBe("Nothing is running right now");
    expect((await c.listInvestigations({ limit: 500 })).items.length).toBe(before);
  });

  it("lists an in-flight simulated run, and offers scenarios for an unknown incident", async () => {
    const c = client();
    const started = await c.ask({ question: "Payment API is returning HTTP 500 in production" });
    expect(started.investigation_id).toMatch(/^inv-/);
    const running = await c.ask({ question: "what's running?" });
    const item = running.answer?.items[0];
    expect(item?.type === "running_investigation" && item.id).toBe(started.investigation_id);
    const unknown = await c.ask({ question: "hello world" });
    expect(unknown.kind).toBe("incident");
    expect(unknown.investigation_id ?? null).toBeNull();
    expect(unknown.answer?.items.map((i) => i.type === "suggestion" && i.scenario)).toContain("S1");
  });
});
