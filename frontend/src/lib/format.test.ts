import { describe, expect, it } from "vitest";

import {
  formatClock,
  formatDuration,
  formatMetric,
  formatPercent,
  formatRelative,
  humanizeSignal,
} from "./format";

const NOW = Date.parse("2026-09-28T12:00:00Z");

describe("formatRelative", () => {
  it.each([
    ["2026-09-28T11:59:50Z", "just now"],
    ["2026-09-28T11:55:00Z", "5 minutes ago"],
    ["2026-09-28T09:00:00Z", "3 hours ago"],
    ["2026-09-27T10:00:00Z", "yesterday"],
    ["2026-09-21T12:00:00Z", "last week"],
  ])("%s → %s", (iso, expected) => {
    expect(formatRelative(iso, NOW)).toBe(expected);
  });

  it("handles missing and invalid input", () => {
    expect(formatRelative(null, NOW)).toBe("—");
    expect(formatRelative("not a date", NOW)).toBe("—");
  });
});

describe("formatDuration", () => {
  it.each([
    [420, "420 ms"],
    [8420, "8.4 s"],
    [42_000, "42 s"],
    [150_000, "2 min 30 s"],
    [120_000, "2 min"],
    [3_900_000, "1 h 5 min"],
  ])("%d ms → %s", (ms, expected) => expect(formatDuration(ms)).toBe(expected));

  it("renders a dash when unknown", () => expect(formatDuration(null)).toBe("—"));
});

describe("other formatters", () => {
  it("formats percentages and clock times", () => {
    expect(formatPercent(0.91)).toBe("91%");
    expect(formatPercent(undefined)).toBe("—");
    expect(formatClock("2026-09-28T10:10:07Z")).toBe("10:10:07");
  });

  it("humanizes signal names with acronyms", () => {
    expect(humanizeSignal("db_timeout_errors_up")).toBe("DB timeout errors up");
    expect(humanizeSignal("oom_killed")).toBe("OOM killed");
  });

  it("formats metric values by unit", () => {
    expect(formatMetric(24.6, "percent")).toBe("25%");
    expect(formatMetric(0.5, "percent")).toBe("0.5%");
    expect(formatMetric(0.21, "seconds")).toBe("210 ms");
    expect(formatMetric(9.48, "seconds")).toBe("9.48 s");
    expect(formatMetric(0, "bool")).toBe("down");
  });
});
