import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ConfidenceMeter, SeverityBadge, StatusBadge } from "./status";

describe("status components", () => {
  it("renders the status label next to its icon (never colour alone)", () => {
    const { container } = render(<StatusBadge status="needs_clarification" />);
    expect(screen.getByText("Needs input")).toBeInTheDocument();
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("renders severities", () => {
    render(<SeverityBadge severity="critical" />);
    expect(screen.getByText("Critical")).toBeInTheDocument();
  });

  it("exposes confidence as an accessible meter", () => {
    render(<ConfidenceMeter value={0.91} />);
    const meter = screen.getByRole("meter", { name: "Confidence" });
    expect(meter).toHaveAttribute("aria-valuenow", "91");
    expect(screen.getByText("91%")).toBeInTheDocument();
  });

  it("shows n/a without a confidence", () => {
    render(<ConfidenceMeter value={0} />);
    expect(screen.getByText("n/a")).toBeInTheDocument();
  });
});
