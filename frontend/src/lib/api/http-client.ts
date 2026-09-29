import { z } from "zod";

import {
  ApiError,
  type ApiClient,
  type Decision,
  type InvestigationFilters,
  type StreamHandlers,
} from "./client";
import { parseOrThrow } from "./parse";
import {
  AgentList,
  ApiErrorBody,
  Approval,
  ApprovalList,
  AskResponse,
  CreateInvestigationResponse,
  DashboardSummary,
  Health,
  IntegrationList,
  IntegrationSaved,
  IntegrationTestResult,
  Investigation,
  InvestigationPage,
  LiveEvent,
  ScenarioList,
  ServiceList,
  type AskRequest,
  type CreateInvestigationRequest,
  type IntegrationUpdate,
} from "./schemas";

const EVENT_TYPES = LiveEvent.options.map((o) => o.shape.type.value);
const TERMINAL = new Set(["investigation_finished"]);

export interface HttpClientOptions {
  baseUrl: string;
  fetchImpl?: typeof fetch;
  eventSourceImpl?: typeof EventSource;
  /** Reconnect backoff (ms): min(max, base * 2^attempt). */
  backoff?: { base: number; max: number };
}

/** Talks to the real API (docs/api/contract.md): REST + SSE. */
export class HttpClient implements ApiClient {
  readonly kind = "http" as const;
  private readonly base: string;
  private readonly fetchImpl: typeof fetch;
  private readonly es: typeof EventSource | undefined;
  private readonly backoff: { base: number; max: number };

  constructor(opts: HttpClientOptions) {
    this.base = opts.baseUrl.replace(/\/$/, "");
    this.fetchImpl = opts.fetchImpl ?? ((...args) => fetch(...args));
    this.es =
      opts.eventSourceImpl ?? (typeof EventSource === "undefined" ? undefined : EventSource);
    this.backoff = opts.backoff ?? { base: 500, max: 10_000 };
  }

  private async request(path: string, init?: RequestInit): Promise<Response> {
    let res: Response;
    try {
      res = await this.fetchImpl(`${this.base}${path}`, {
        ...init,
        headers: {
          Accept: "application/json",
          ...(init?.body ? { "Content-Type": "application/json" } : {}),
          ...init?.headers,
        },
      });
    } catch (err) {
      throw new ApiError(
        `Cannot reach the API at ${this.base} (${(err as Error).message})`,
        0,
        "network",
      );
    }
    if (!res.ok) {
      let message = `${res.status} ${res.statusText}`;
      let code = "http_error";
      try {
        const body = ApiErrorBody.safeParse(await res.json());
        if (body.success) {
          message = body.data.error.message;
          code = body.data.error.code;
        }
      } catch {
        // non-JSON error body: keep the status line
      }
      throw new ApiError(message, res.status, code);
    }
    return res;
  }

  private async json<S extends z.ZodType>(
    schema: S,
    path: string,
    init?: RequestInit,
  ): Promise<z.infer<S>> {
    const res = await this.request(path, init);
    return parseOrThrow(schema, await res.json(), `${init?.method ?? "GET"} ${path}`);
  }

  private post(path: string, body: unknown = {}): Promise<Response> {
    return this.request(path, { method: "POST", body: JSON.stringify(body) });
  }

  health() {
    return this.json(Health, "/health");
  }
  services() {
    return this.json(ServiceList, "/services");
  }
  agents() {
    return this.json(AgentList, "/agents");
  }
  dashboard(days = 14) {
    return this.json(DashboardSummary, `/dashboard/summary?days=${days}`);
  }
  listInvestigations(f: InvestigationFilters = {}) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(f))
      if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    const s = qs.toString();
    return this.json(InvestigationPage, `/investigations${s ? `?${s}` : ""}`);
  }
  getInvestigation(id: string) {
    return this.json(Investigation, `/investigations/${encodeURIComponent(id)}`);
  }
  async createInvestigation(req: CreateInvestigationRequest) {
    const res = await this.post("/investigations", req);
    return parseOrThrow(CreateInvestigationResponse, await res.json(), "POST /investigations");
  }
  async ask(req: AskRequest) {
    const res = await this.post("/ask", req);
    return parseOrThrow(AskResponse, await res.json(), "POST /ask");
  }
  async clarify(id: string, answer: string) {
    await this.post(`/investigations/${encodeURIComponent(id)}/clarify`, { answer });
  }
  async cancel(id: string) {
    await this.post(`/investigations/${encodeURIComponent(id)}/cancel`);
  }
  async reportMarkdown(id: string) {
    const res = await this.request(`/investigations/${encodeURIComponent(id)}/report.md`, {
      headers: { Accept: "text/markdown" },
    });
    return res.text();
  }
  async draftTicket(id: string) {
    const res = await this.post(`/investigations/${encodeURIComponent(id)}/tickets/draft`);
    return parseOrThrow(
      z.object({ approval_id: z.string() }),
      await res.json(),
      "POST tickets/draft",
    );
  }
  approvals(status?: string) {
    return this.json(
      ApprovalList,
      `/approvals${status ? `?status=${encodeURIComponent(status)}` : ""}`,
    );
  }
  async decide(id: string, decision: Decision, body: { by: string; comment?: string }) {
    const res = await this.post(`/approvals/${encodeURIComponent(id)}/${decision}`, body);
    return parseOrThrow(Approval, await res.json(), `POST /approvals/${decision}`);
  }
  scenarios() {
    return this.json(ScenarioList, "/scenarios");
  }
  async injectScenario(id: string) {
    await this.post(`/scenarios/${encodeURIComponent(id)}/inject`);
  }
  async revertScenarios() {
    await this.post("/scenarios/revert");
  }
  integrations() {
    return this.json(IntegrationList, "/integrations");
  }
  saveIntegration(capability: string, update: IntegrationUpdate) {
    return this.json(IntegrationSaved, `/integrations/${encodeURIComponent(capability)}`, {
      method: "PUT",
      body: JSON.stringify(update),
    });
  }
  testIntegration(capability: string, draft?: IntegrationUpdate) {
    return this.json(
      IntegrationTestResult,
      `/integrations/${encodeURIComponent(capability)}/test`,
      { method: "POST", body: JSON.stringify(draft ?? {}) },
    );
  }

  /**
   * SSE via EventSource. The browser reconnects on its own and sends `Last-Event-ID`; when the
   * connection is closed for good (e.g. a 5xx), we reopen it with exponential backoff and pass the
   * last seq as `?last_event_id=` (EventSource cannot set headers). Events are de-duplicated by seq.
   */
  subscribe(id: string, h: StreamHandlers, lastSeq = 0): () => void {
    const ES = this.es;
    if (!ES) {
      h.onError?.(new Error("EventSource is not available in this environment"));
      return () => undefined;
    }
    let seq = lastSeq;
    let source: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let stopped = false;

    const handle = (msg: MessageEvent<string>) => {
      let raw: unknown;
      try {
        raw = JSON.parse(msg.data);
      } catch {
        return;
      }
      const parsed = LiveEvent.safeParse(raw);
      if (!parsed.success) {
        h.onError?.(
          new Error(
            `Ignored an event that doesn't match the contract: ${z.prettifyError(parsed.error)}`,
          ),
        );
        return;
      }
      const event = parsed.data;
      if (event.seq <= seq) return; // duplicate after a reconnect
      seq = event.seq;
      attempt = 0;
      h.onEvent(event);
      if (TERMINAL.has(event.type)) close();
    };

    const open = () => {
      if (stopped) return;
      h.onState?.(attempt === 0 ? "connecting" : "reconnecting");
      const url = `${this.base}/investigations/${encodeURIComponent(id)}/events${seq > 0 ? `?last_event_id=${seq}` : ""}`;
      const es = new ES(url);
      source = es;
      es.onopen = () => h.onState?.("open");
      es.onmessage = handle;
      for (const t of EVENT_TYPES) es.addEventListener(t, handle as EventListener);
      es.onerror = () => {
        if (stopped) return;
        if (es.readyState === ES.CLOSED) {
          es.close();
          const delay = Math.min(this.backoff.max, this.backoff.base * 2 ** attempt);
          attempt += 1;
          h.onState?.("reconnecting");
          timer = setTimeout(open, delay);
        } else {
          h.onState?.("reconnecting");
        }
      };
    };

    const close = () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      source?.close();
      h.onState?.("closed");
    };

    open();
    return close;
  }
}
