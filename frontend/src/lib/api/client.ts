import type {
  Agent,
  Approval,
  AskRequest,
  AskResponse,
  CreateInvestigationRequest,
  CreateInvestigationResponse,
  DashboardSummary,
  Health,
  Investigation,
  InvestigationPage,
  InvestigationStatus,
  LiveEvent,
  Scenario,
  Service,
  Severity,
} from "./schemas";

export interface InvestigationFilters {
  status?: InvestigationStatus | "";
  service?: string;
  severity?: Severity | "";
  q?: string;
  limit?: number;
  cursor?: string | null;
}

export type StreamState = "connecting" | "open" | "reconnecting" | "closed";

export interface StreamHandlers {
  onEvent: (event: LiveEvent) => void;
  onState?: (state: StreamState) => void;
  onError?: (error: Error) => void;
}

export type Decision = "approve" | "deny";

/** Everything the UI needs from the backend (docs/api/contract.md). */
export interface ApiClient {
  readonly kind: "http" | "demo";
  health(): Promise<Health>;
  services(): Promise<Service[]>;
  agents(): Promise<Agent[]>;
  dashboard(days?: number): Promise<DashboardSummary>;
  listInvestigations(filters?: InvestigationFilters): Promise<InvestigationPage>;
  getInvestigation(id: string): Promise<Investigation>;
  createInvestigation(req: CreateInvestigationRequest): Promise<CreateInvestigationResponse>;
  /** The chat box (PR-041): answers a platform question, or starts an investigation. */
  ask(req: AskRequest): Promise<AskResponse>;
  clarify(id: string, answer: string): Promise<void>;
  cancel(id: string): Promise<void>;
  reportMarkdown(id: string): Promise<string>;
  draftTicket(id: string): Promise<{ approval_id: string }>;
  approvals(status?: string): Promise<Approval[]>;
  decide(id: string, decision: Decision, body: { by: string; comment?: string }): Promise<Approval>;
  scenarios(): Promise<Scenario[]>;
  injectScenario(id: string): Promise<void>;
  revertScenarios(): Promise<void>;
  /** Subscribe to the live event stream. Returns an unsubscribe function. */
  subscribe(id: string, handlers: StreamHandlers, lastSeq?: number): () => void;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string = "error",
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Thrown when a payload doesn't match the contract (a backend/UI drift, not a user error). */
export class ContractError extends Error {
  constructor(
    readonly endpoint: string,
    readonly details: string,
  ) {
    super(`Unexpected response from ${endpoint}: ${details}`);
    this.name = "ContractError";
  }
}
