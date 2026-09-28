/** Demo only: map a free-text question to one of the recorded scenarios (S0–S5). */

export type DemoScenarioId = "S0" | "S1" | "S2" | "S3" | "S4" | "S5";
export type Route =
  { scenario: DemoScenarioId; service: string } | { scenario: "clarify"; candidates: string[] };

const ALIASES: [string, RegExp][] = [
  ["payment-service", /\b(payment-service|payments?|pay|payments?-api)\b/],
  ["order-service", /\b(order-service|orders?|checkout|orders-api)\b/],
  ["user-service", /\b(user-service|users?|auth|login|logins|accounts?)\b/],
  ["inventory-service", /\b(inventory-service|inventory|stock)\b/],
];

/** The service mentioned first in the question, if any (UC-13: never invent one). */
export function detectService(question: string): string | null {
  const q = question.toLowerCase();
  let best: { svc: string; idx: number } | null = null;
  for (const [svc, re] of ALIASES) {
    const m = re.exec(q);
    if (m && (best === null || m.index < best.idx)) best = { svc, idx: m.index };
  }
  return best?.svc ?? null;
}

export const CLARIFY_CANDIDATES = ["payment-service", "order-service", "user-service"];

export function routeQuestion(question: string, service?: string | null): Route {
  const q = question.toLowerCase();
  const svc = service || detectService(q);
  if (!svc) {
    if (/\b(redis|cache)\b/.test(q)) return { scenario: "S5", service: "payment-service" };
    return { scenario: "clarify", candidates: CLARIFY_CANDIDATES };
  }
  switch (svc) {
    case "payment-service":
      if (/anything wrong|healthy|health check|is .* ok/.test(q))
        return { scenario: "S0", service: svc };
      if (/\b(slow|latency|redis|cache)\b/.test(q)) return { scenario: "S5", service: svc };
      return { scenario: "S1", service: svc };
    case "order-service":
      if (/tim(e|ing) ?out|timeouts?|504|slow|inventory|latency/.test(q))
        return { scenario: "S3", service: svc };
      return { scenario: "S2", service: svc };
    case "user-service":
      return { scenario: "S4", service: svc };
    case "inventory-service":
      return { scenario: "S3", service: "order-service" };
    default:
      return { scenario: "clarify", candidates: CLARIFY_CANDIDATES };
  }
}
