import type { FaultStatus } from "@/lib/api/schemas";

/**
 * Fault injection progress (Scenarios page). `POST /scenarios/revert` only *starts* the revert
 * (202, a background thread: rollouts take minutes), so the UI polls `GET /scenarios/status` until
 * the API confirms the outcome instead of claiming "Reverted" at once (live-demo regression).
 */
export interface WaitOptions {
  intervalMs?: number;
  timeoutMs?: number;
  /** Called with every polled status (the page updates its badges from it). */
  onStatus?: (status: FaultStatus) => void;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
}

export const POLL_MS = 2_000;
/** The backend waits up to 180 s per rollout (redis + 4 services, partly in parallel). */
export const REVERT_TIMEOUT_MS = 6 * 60_000;
export const INJECT_TIMEOUT_MS = 30_000;

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

async function poll(
  getStatus: () => Promise<FaultStatus>,
  done: (s: FaultStatus) => boolean,
  timeoutMessage: string,
  { intervalMs = POLL_MS, timeoutMs, onStatus, sleep = defaultSleep, now = Date.now }: WaitOptions,
): Promise<FaultStatus> {
  const deadline = now() + (timeoutMs ?? REVERT_TIMEOUT_MS);
  for (;;) {
    const status = await getStatus();
    onStatus?.(status);
    if (done(status)) return status;
    if (now() >= deadline) throw new Error(timeoutMessage);
    await sleep(intervalMs);
  }
}

/** Resolves once the background revert finished; rejects with the API's error message. */
export async function waitForRevert(
  getStatus: () => Promise<FaultStatus>,
  opts: WaitOptions = {},
): Promise<FaultStatus> {
  const status = await poll(
    getStatus,
    (s) => !s.reverting,
    "Still reverting after several minutes; check `make fault-status` and the cluster.",
    { timeoutMs: REVERT_TIMEOUT_MS, ...opts },
  );
  if (status.last_error) throw new Error(status.last_error);
  if (status.active) throw new Error(`${status.active} is still active after the revert.`);
  return status;
}

/** Resolves once the API reports `scenario` as the active fault. */
export function waitForInject(
  scenario: string,
  getStatus: () => Promise<FaultStatus>,
  opts: WaitOptions = {},
): Promise<FaultStatus> {
  return poll(
    getStatus,
    (s) => s.active?.toUpperCase() === scenario.toUpperCase(),
    `${scenario} is not reported active yet; check \`make fault-status\`.`,
    { timeoutMs: INJECT_TIMEOUT_MS, ...opts },
  );
}
