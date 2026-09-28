import { describe, expect, it } from "vitest";

import { createSequencer } from "./shortcuts";

describe("createSequencer", () => {
  const bindings = ["g d", "g i", "n", "?"];

  it("matches chords within the timeout", () => {
    let t = 0;
    const seq = createSequencer(bindings, 1000, () => t);
    expect(seq({ key: "g" })).toBeNull();
    t = 500;
    expect(seq({ key: "d" })).toBe("g d");
  });

  it("drops an expired chord", () => {
    let t = 0;
    const seq = createSequencer(bindings, 1000, () => t);
    seq({ key: "g" });
    t = 2000;
    expect(seq({ key: "i" })).toBeNull();
  });

  it("matches single keys and ignores modifiers", () => {
    const seq = createSequencer(bindings);
    expect(seq({ key: "n" })).toBe("n");
    expect(seq({ key: "?" })).toBe("?");
    expect(seq({ key: "n", metaKey: true })).toBeNull();
  });

  it("ignores keys typed into inputs", () => {
    const seq = createSequencer(bindings);
    const input = document.createElement("input");
    expect(seq({ key: "n", target: input })).toBeNull();
  });
});
