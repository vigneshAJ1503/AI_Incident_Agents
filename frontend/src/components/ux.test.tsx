import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Sparkline } from "@/components/charts/sparkline";
import { EventLog } from "@/components/investigation/event-log";
import { EmptyState } from "@/components/states";
import { readDensity, setDensity } from "@/lib/density";
import { stepTrail } from "@/lib/evidence";
import type { LogEntry } from "@/lib/live/reducer";

const entry = (seq: number): LogEntry => ({
  seq,
  timestamp: "2026-09-28T10:10:00Z",
  type: "heartbeat",
  agent: null,
  text: `event ${seq}`,
  tone: "info",
});

describe("evidence drawer trail", () => {
  it("walks forward and back with wrap-around", () => {
    const t = ["a", "b", "c"];
    expect(stepTrail(t, "a", 1)).toBe("b");
    expect(stepTrail(t, "c", 1)).toBe("a");
    expect(stepTrail(t, "a", -1)).toBe("c");
    expect(stepTrail(t, "zz", 1)).toBe("a");
    expect(stepTrail([], "a", 1)).toBeNull();
  });
});

describe("density", () => {
  it("defaults to comfortable and persists compact on <html>", () => {
    localStorage.clear();
    expect(readDensity()).toBe("comfortable");
    setDensity("compact");
    expect(readDensity()).toBe("compact");
    expect(document.documentElement.dataset.density).toBe("compact");
    setDensity("comfortable");
  });
});

describe("event log", () => {
  it("pauses auto-scroll and counts the events that arrived meanwhile", () => {
    const { rerender } = render(<EventLog entries={[entry(1), entry(2)]} />);
    const toggle = screen.getByTestId("log-pause");
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("log")).toHaveAttribute("aria-live", "off");
    rerender(<EventLog entries={[entry(1), entry(2), entry(3), entry(4)]} />);
    expect(screen.getByTestId("log-unseen")).toHaveTextContent("2 new");
    fireEvent.click(screen.getByTestId("log-unseen"));
    expect(screen.getByTestId("log-pause")).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryByTestId("log-unseen")).toBeNull();
  });
});

describe("sparkline and empty state", () => {
  it("labels the trend for assistive tech", () => {
    render(<Sparkline values={[1, 3, 2]} label="Investigations per day" />);
    expect(screen.getByRole("img", { name: "Investigations per day" })).toBeInTheDocument();
  });

  it("renders the next action", () => {
    render(<EmptyState title="Nothing here" action={<button type="button">Do it</button>} />);
    expect(screen.getByText("Nothing here")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Do it" })).toBeInTheDocument();
  });
});
