/**
 * Settings → Integrations form logic (PR-046), kept pure so it is unit-tested: labels, how a
 * field's value is shown in an input, parsing it back, and the minimal update body.
 */
import type {
  FieldValue,
  Integration,
  IntegrationField,
  IntegrationSecret,
  IntegrationUpdate,
} from "@/lib/api/schemas";

/** The capability cards, in the API's order, with what each one is for. */
export const CAPABILITY_INFO: Record<string, { title: string; vendors: string }> = {
  logs: { title: "Logs", vendors: "Elasticsearch · Loki" },
  metrics: { title: "Metrics", vendors: "Prometheus-compatible" },
  alerts: { title: "Alerts", vendors: "Alertmanager" },
  k8s: { title: "Kubernetes", vendors: "any cluster, read-only RBAC" },
  code: { title: "Code", vendors: "GitHub / GitLab via git" },
  tickets: { title: "Tickets", vendors: "Jira · mock" },
  knowledge: { title: "Knowledge", vendors: "Markdown runbooks" },
};

export const capabilityTitle = (capability: string) =>
  CAPABILITY_INFO[capability]?.title ?? capability;

/** `settings.fields.level` → `fields.level`; `mcp.url` → `MCP URL`. */
export function fieldLabel(key: string): string {
  const named: Record<string, string> = {
    "mcp.url": "MCP server URL",
    "mcp.timeout_s": "Timeout (s)",
    "limits.max_results": "Max results",
    "limits.query_timeout_s": "Query timeout (s)",
    "limits.max_time_range_hours": "Max time range (h)",
  };
  return named[key] ?? key.replace(/^settings\./, "");
}

export type FieldGroup = "connection" | "settings" | "limits";

export function fieldGroup(key: string): FieldGroup {
  if (key.startsWith("mcp.")) return "connection";
  if (key.startsWith("limits.")) return "limits";
  return "settings";
}

/** What an input shows for a value (lists as comma-separated text). */
export function toInput(field: IntegrationField, value: unknown): string {
  if (value === null || value === undefined) return "";
  if (field.type === "list" && Array.isArray(value)) return value.join(", ");
  if (field.type === "json") return JSON.stringify(value);
  return String(value);
}

/** An input's text back to a typed value; `null` = empty (back to the profile's value). */
export function fromInput(field: IntegrationField, text: string): FieldValue {
  const trimmed = text.trim();
  if (field.type === "number") return trimmed === "" ? null : Number(trimmed);
  if (field.type === "list") {
    const items = trimmed === "" ? [] : trimmed.split(",").map((s) => s.trim());
    const numeric =
      Array.isArray(field.default) &&
      field.default.length > 0 &&
      field.default.every((v) => typeof v === "number");
    return items.filter(Boolean).map((s) => (numeric ? Number(s) : s));
  }
  if (field.type === "string") return trimmed === "" && field.default === null ? null : text;
  return text;
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

export interface FormState {
  enabled: boolean;
  provider: string | null;
  /** Edited fields only: key → typed value (`null` = reset to the profile). */
  fields: Record<string, FieldValue>;
  /** Secrets to set (header → value) or clear (header → null). */
  secrets: Record<string, string | null>;
}

export const initialForm = (item: Integration): FormState => ({
  enabled: item.enabled,
  provider: item.provider,
  fields: {},
  secrets: {},
});

/** The minimal PUT body for the form; `null` when nothing changed. */
export function buildUpdate(item: Integration, form: FormState): IntegrationUpdate | null {
  const update: IntegrationUpdate = {};
  if (form.enabled !== item.enabled) update.enabled = form.enabled;
  if (form.provider && form.provider !== item.provider) update.provider = form.provider;
  const fields: Record<string, FieldValue> = {};
  for (const [key, value] of Object.entries(form.fields)) {
    const field = item.fields.find((f) => f.key === key);
    if (!field?.editable) continue;
    if (value === null ? field.overridden : !same(value, field.value)) fields[key] = value;
  }
  if (Object.keys(fields).length) update.fields = fields;
  const secrets = Object.fromEntries(
    Object.entries(form.secrets).filter(([name, v]) => name.trim() && (v === null || v !== "")),
  );
  if (Object.keys(secrets).length) update.secrets = secrets;
  return Object.keys(update).length ? update : null;
}

/** `••••9f2c`, or `configured` when the value is too short to hint at. Never the value. */
export function maskedSecret(secret: IntegrationSecret): string {
  if (!secret.configured) return "not set";
  return secret.last4 ? `••••${secret.last4}` : "configured";
}

export const HEADER_NAME = /^[A-Za-z][A-Za-z0-9-]{0,63}$/;
