import { describe, expect, it, vi } from "vitest";

import type { FaultStatus } from "@/lib/api/schemas";
import { waitForInject, waitForRevert } from "@/lib/faults";

/** A scripted `GET /scenarios/status`: returns the statuses in order, then repeats the last. */
function scripted(...statuses: FaultStatus[]) {
  let i = 0;
  return vi.fn(async () => statuses[Math.min(i++, statuses.length - 1)]!);
}

const st = (active: string | null, reverting: boolean, last_error: string | null = null) => ({
  active,
  reverting,
  last_error,
});

// a fake clock: each sleep advances it, so timeouts are deterministic and instant
function clock() {
  let t = 0;
  return { now: () => t, sleep: async (ms: number) => void (t += ms) };
}

describe("waitForRevert (live-demo regression: 'Reverted' shown while still reverting)", () => {
  it("keeps polling while the background revert runs, then resolves", async () => {
    const getStatus = scripted(st("S1", true), st("S1", true), st(null, false));
    const seen: FaultStatus[] = [];
    const done = await waitForRevert(getStatus, { ...clock(), onStatus: (s) => seen.push(s) });
    expect(done).toEqual(st(null, false));
    expect(getStatus).toHaveBeenCalledTimes(3);
    expect(seen.map((s) => s.reverting)).toEqual([true, true, false]);
  });

  it("rejects with the API's error when the revert failed", async () => {
    const getStatus = scripted(st("S1", true), st("S1", false, "rollout status timed out"));
    await expect(waitForRevert(getStatus, clock())).rejects.toThrow("rollout status timed out");
  });

  it("rejects when a scenario is still active after the revert", async () => {
    await expect(waitForRevert(scripted(st("S2", false)), clock())).rejects.toThrow(
      "S2 is still active",
    );
  });

  it("gives up after the timeout instead of spinning forever", async () => {
    const getStatus = scripted(st("S1", true));
    await expect(waitForRevert(getStatus, { ...clock(), timeoutMs: 10_000 })).rejects.toThrow(
      /Still reverting/,
    );
    expect(getStatus.mock.calls.length).toBe(6); // t = 0, 2, 4, 6, 8, 10 s
  });
});

describe("waitForInject", () => {
  it("resolves once the API reports the scenario active (case-insensitive)", async () => {
    const getStatus = scripted(st(null, false), st("s3", false));
    await expect(waitForInject("S3", getStatus, clock())).resolves.toEqual(st("s3", false));
  });

  it("times out when the scenario never shows up as active", async () => {
    await expect(
      waitForInject("S3", scripted(st(null, false)), { ...clock(), timeoutMs: 4_000 }),
    ).rejects.toThrow(/S3 is not reported active/);
  });
});
