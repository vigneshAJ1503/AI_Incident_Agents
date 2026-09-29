import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import health from "@/demo/health.json";

import { ApiError, ContractError } from "./client";
import { HttpClient } from "./http-client";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("HttpClient REST", () => {
  it("parses a valid response and calls the contract path", async () => {
    const fetchImpl = vi.fn(async () => json(health));
    const c = new HttpClient({ baseUrl: "http://api/api/", fetchImpl });
    const h = await c.health();
    expect(h.llm.configured).toBe(false);
    expect(fetchImpl).toHaveBeenCalledWith("http://api/api/health", expect.anything());
  });

  it("builds list query strings without empty filters", async () => {
    const fetchImpl = vi.fn(async (_url: string | URL | Request) =>
      json({ items: [], next_cursor: null }),
    );
    const c = new HttpClient({ baseUrl: "http://api", fetchImpl });
    await c.listInvestigations({ status: "completed", service: "", q: "pool", limit: 20 });
    expect(String(fetchImpl.mock.calls[0]?.[0])).toBe(
      "http://api/investigations?status=completed&q=pool&limit=20",
    );
  });

  it("maps the error envelope to ApiError", async () => {
    const c = new HttpClient({
      baseUrl: "http://api",
      fetchImpl: async () =>
        json({ error: { code: "not_found", message: "Investigation inv-x not found" } }, 404),
    });
    await expect(c.getInvestigation("inv-x")).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      code: "not_found",
      message: "Investigation inv-x not found",
    });
  });

  it("reads the fault status; falls back to the scenario list on an older API", async () => {
    const status = { active: "S1", reverting: true, last_error: null };
    const c = new HttpClient({ baseUrl: "http://api", fetchImpl: async () => json(status) });
    await expect(c.scenarioStatus()).resolves.toEqual(status);

    const scenario = { id: "S1", title: "t", service: "payment-service", active: true };
    const old = new HttpClient({
      baseUrl: "http://api",
      fetchImpl: async (url: string | URL | Request) =>
        String(url).endsWith("/scenarios/status")
          ? json({ error: { code: "not_found", message: "Not Found" } }, 404)
          : json([scenario]),
    });
    await expect(old.scenarioStatus()).resolves.toEqual({
      active: "S1",
      reverting: true,
      last_error: null,
    });
  });

  it("reports network failures as status 0", async () => {
    const c = new HttpClient({
      baseUrl: "http://api",
      fetchImpl: async () => {
        throw new TypeError("fetch failed");
      },
    });
    const err = await c.health().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(0);
  });

  it("rejects payloads that don't match the contract", async () => {
    const c = new HttpClient({
      baseUrl: "http://api",
      fetchImpl: async () => json({ status: "ok" }),
    });
    await expect(c.health()).rejects.toBeInstanceOf(ContractError);
  });
});

// ---- SSE -----------------------------------------------------------------------------------------

class FakeEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  static instances: FakeEventSource[] = [];
  readyState = FakeEventSource.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((e: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, (e: MessageEvent<string>) => void>();
  closed = false;
  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, fn: (e: MessageEvent<string>) => void) {
    this.listeners.set(type, fn);
  }
  close() {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }
  emit(ev: { type: string; [k: string]: unknown }) {
    const fn = this.listeners.get(ev.type);
    fn?.(new MessageEvent("message", { data: JSON.stringify(ev) }));
  }
  fail() {
    this.readyState = FakeEventSource.CLOSED;
    this.onerror?.();
  }
}

const event = (seq: number, type: string, data: unknown = {}) => ({
  type,
  seq,
  investigation_id: "inv-1",
  timestamp: "2026-09-28T10:00:00Z",
  agent: null,
  data,
});

describe("HttpClient SSE", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.useFakeTimers();
  });
  afterEach(() => vi.useRealTimers());

  const client = () =>
    new HttpClient({
      baseUrl: "http://api",
      eventSourceImpl: FakeEventSource as unknown as typeof EventSource,
      backoff: { base: 100, max: 1000 },
    });

  it("delivers events in order, drops duplicates and closes after investigation_finished", () => {
    const seen: number[] = [];
    const states: string[] = [];
    client().subscribe("inv-1", {
      onEvent: (e) => seen.push(e.seq),
      onState: (s) => states.push(s),
    });
    const es = FakeEventSource.instances[0]!;
    expect(es.url).toBe("http://api/investigations/inv-1/events");
    es.emit(event(1, "investigation_started", { question: "q" }));
    es.emit(event(1, "investigation_started", { question: "q" }));
    es.emit(event(2, "heartbeat"));
    es.emit(event(3, "investigation_finished", { status: "completed", duration_ms: 10 }));
    expect(seen).toEqual([1, 2, 3]);
    expect(es.closed).toBe(true);
    expect(states.at(-1)).toBe("closed");
  });

  it("reconnects with backoff and resumes from the last seq", () => {
    const seen: number[] = [];
    client().subscribe("inv-1", { onEvent: (e) => seen.push(e.seq) });
    const first = FakeEventSource.instances[0]!;
    first.emit(event(1, "investigation_started", { question: "q" }));
    first.emit(event(2, "rca_started"));
    first.fail();
    expect(FakeEventSource.instances).toHaveLength(1);
    vi.advanceTimersByTime(100);
    const second = FakeEventSource.instances[1]!;
    expect(second.url).toBe("http://api/investigations/inv-1/events?last_event_id=2");
    second.emit(event(2, "rca_started"));
    second.emit(event(3, "heartbeat"));
    expect(seen).toEqual([1, 2, 3]);
  });

  it("reports events that don't match the contract without delivering them", () => {
    const errors: string[] = [];
    const seen: number[] = [];
    client().subscribe("inv-1", {
      onEvent: (e) => seen.push(e.seq),
      onError: (e) => errors.push(e.message),
    });
    FakeEventSource.instances[0]!.emit({ ...event(1, "agent_started"), data: { nope: true } });
    expect(seen).toEqual([]);
    expect(errors[0]).toContain("contract");
  });

  it("unsubscribe stops reconnecting", () => {
    const stop = client().subscribe("inv-1", { onEvent: () => undefined });
    stop();
    FakeEventSource.instances[0]!.fail();
    vi.advanceTimersByTime(5000);
    expect(FakeEventSource.instances).toHaveLength(1);
  });
});
