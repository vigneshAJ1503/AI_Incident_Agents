/**
 * Demo mode only: the rule-based intent classifier of the backend
 * (backend/src/aiops/orchestrator/intent.py), ported so the static demo answers platform
 * questions without an API. The real API classifies on the server (rules + the fast LLM role).
 */
import { detectService } from "@/lib/demo/route";

export type PlatformIntent =
  "agents_running" | "agents_catalog" | "health" | "recent_investigations" | "help";
export type Intent = PlatformIntent | "incident";

export interface Classification {
  kind: "platform" | "incident";
  intent: Intent;
  service: string | null;
  /** false = no rule matched (the API would ask the LLM, else default to incident). */
  sure: boolean;
}

const I = "i";
const HISTORY = "(incidents?|investigations?|outages?|rcas?|root causes?|postmortems?)";
const RUN_WORDS = "(running|in progress|in[- ]flight|underway|active|busy|working)";
const AGENTS = "(agents?|investigations?|runs?|jobs?)";

const HELP = [
  /^\s*(help|\?+|examples?|usage|how does this work|what is this)\s*[?.!]*\s*$/i,
  /\bwhat (should|can|could) i (ask|type|say)\b/i,
  /\b(give me|show me|any) (some )?(example|sample) (questions?|prompts?)\b/i,
];
const RUNNING = [
  /\bwhat('?s|\s+is|\s+are)?\s+(currently\s+|still\s+|now\s+)?(running|in progress|in[- ]flight|underway)\b/i,
  new RegExp(
    `\\b(what|which|any|are there( any)?|show|list)\\s+([\\w-]+\\s+){0,2}?${AGENTS}\\s+(that\\s+|which\\s+)?(are\\s+|is\\s+)?(still\\s+|currently\\s+|now\\s+)?${RUN_WORDS}`,
    I,
  ),
  new RegExp(
    `\\b(is|are)\\s+(the\\s+|any\\s+|my\\s+)?([\\w-]+\\s+){0,2}?${AGENTS}\\s+(still\\s+|currently\\s+)?${RUN_WORDS}\\b`,
    I,
  ),
  new RegExp(`\\b(running|active|ongoing|current|in[- ]progress)\\s+${AGENTS}\\b`, I),
  new RegExp(`\\b(status|progress) of (the |my )?(running |current )?${AGENTS}\\b`, I),
];
const RECENT = [
  new RegExp(
    `\\b(recent|last|latest|previous|past|prior|earlier|older)\\s+([\\w-]+\\s+){0,3}?${HISTORY}\\b`,
    I,
  ),
  new RegExp(
    `\\b(show|list|find|search|get)\\s+(me\\s+)?(the\\s+|all\\s+)?([\\w-]+\\s+){0,3}?${HISTORY}\\b`,
    I,
  ),
  /\bwhat happened (with|to|in|during) (the )?(last|latest|recent|previous|yesterday)/i,
  new RegExp(`\\bhow many\\s+([\\w-]+\\s+){0,2}?${HISTORY}\\b`, I),
];
const CATALOG = [
  /\b(which|what)\s+(\w+\s+){0,2}?agents\b/i,
  /\b(list|show|describe)\s+(me\s+)?(the\s+|all\s+|your\s+)?(\w+\s+)?agents\b/i,
  /\bhow many agents\b/i,
  /\bwhat (can|do) you (check|do|look at|investigate|inspect|monitor|cover)\b/i,
  /\bwhat (are )?(your|the) (capabilities|tools|data sources|integrations)\b/i,
  /\bwhich (data sources|tools|capabilities|integrations)\b/i,
];
const HEALTH = [
  /^\s*what('?s|\s+is|\s+are)\s+(currently\s+)?(down|broken|unhealthy|offline|not working|unreachable)\b/i,
  /\b(platform|system) (health|status)\b/i,
  /^\s*(health|health ?check|status)\s*[?.!]*\s*$/i,
  /\b(is|are) (the )?(llm|model|ai)\b/i,
  /\bwhich (llm|model|provider)\b/i,
  /\bis (fault injection|faults?) (enabled|on|allowed)\b/i,
];
const STATE =
  /\b(is|are|isn'?t|aren'?t)\b.*\b(up|down|configured|reachable|unreachable|healthy|unhealthy|working|available|connected|online|offline|ok|enabled|disabled|broken|running)\b/i;
const COMPONENTS =
  /\b(llm|model|ai|api|backend|store|postgres|mcp|prometheus|elasticsearch|loki|alertmanager|kubernetes|git|jira|logs|metrics|alerts|k8s|tickets|knowledge|capabilities|data sources|platform|system)\b/i;
const SYMPTOMS =
  /\b(5\d\d|5xx|errors?|failing|fails|failed|failures?|broken|exceptions?|slow|slowness|latency|time[ds]?[ -]?outs?|timing out|down|unavailable|outage|unreachable|restart(s|ing)?|crash(es|ing)?|oom\w*|memory|leak)\b/i;
const AGENT_NOUN = /\b(agents?|investigations?)\b/i;

export const hasSymptoms = (q: string) => SYMPTOMS.test(q);

export function classify(question: string): Classification {
  const q = question.replace(/\s+/g, " ").trim();
  const service = detectService(q);
  const platform = (intent: PlatformIntent): Classification => ({
    kind: "platform",
    intent,
    service,
    sure: true,
  });
  const any = (res: RegExp[]) => res.some((r) => r.test(q));

  if (any(HELP)) return platform("help");
  if (any(RUNNING) && (!service || AGENT_NOUN.test(q))) return platform("agents_running");
  if (any(RECENT)) return platform("recent_investigations");
  const symptoms = hasSymptoms(q);
  if (service && symptoms) return { kind: "incident", intent: "incident", service, sure: true };
  if (any(CATALOG)) return platform("agents_catalog");
  if (!service && any(HEALTH)) return platform("health");
  if (!service && STATE.test(q) && COMPONENTS.test(q)) return platform("health");
  return { kind: "incident", intent: "incident", service, sure: Boolean(service || symptoms) };
}
