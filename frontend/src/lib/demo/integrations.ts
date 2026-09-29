/**
 * Demo-mode Settings → Integrations (PR-046): the same rules as the API, in the tab only.
 * Secrets are masked like the real API does: the value is dropped at once, only "configured" and
 * (for long values) the last 4 characters are kept.
 */
import { ApiError } from "@/lib/api/client";
import type {
  FieldChange,
  FieldValue,
  Integration,
  IntegrationSaved,
  IntegrationTestResult,
  IntegrationUpdate,
} from "@/lib/api/schemas";

const HEADER = /^[A-Za-z][A-Za-z0-9-]{0,63}$/;

/** The last 4 characters of a long secret, like `core/secrets.py`. */
export function secretHint(value: string): string | null {
  return value.length >= 12 ? value.slice(-4) : null;
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

function checkValue(key: string, type: string, value: FieldValue): void {
  const bad = (msg: string) => new ApiError(`field ${key} ${msg}`, 422, "invalid_integration");
  if (key === "mcp.url") {
    if (typeof value !== "string" || !/^https?:\/\/[^\s/]+/.test(value))
      throw bad("must be an http(s) URL, e.g. http://logs-mcp:8101/mcp");
    if (/^https?:\/\/[^/]*@/.test(value)) throw bad("must not contain credentials");
    return;
  }
  if (type === "number" && (typeof value !== "number" || Number.isNaN(value)))
    throw bad("must be a number");
  if (type === "boolean" && typeof value !== "boolean") throw bad("must be true or false");
  if (type === "list" && !Array.isArray(value)) throw bad("must be a list");
  if (type === "string" && typeof value !== "string") throw bad("must be text");
}

/** Apply an update to a demo integration (validated like the API; secrets masked). */
export function applyDemoUpdate(
  item: Integration,
  update: IntegrationUpdate,
  actor: string,
  now: string,
  secretsEnabled: boolean,
): IntegrationSaved {
  if (!item.configured)
    throw new ApiError(`capability ${item.capability} is not configured`, 409, "not_configured");
  const changes: FieldChange[] = [];
  let next: Integration = structuredClone(item);
  if (update.reset) {
    for (const key of next.overridden) changes.push({ field: key, change: "reverted" });
    next = {
      ...next,
      fields: next.fields.map((f) => ({ ...f, value: f.default, overridden: false })),
      secrets: next.secrets.filter((s) => s.source !== "ui"),
      overridden: [],
    };
  }
  if (update.provider !== undefined && update.provider !== next.provider) {
    if (!next.providers.includes(update.provider))
      throw new ApiError(
        `provider '${update.provider}' is not an implemented ${item.capability} provider`,
        422,
        "invalid_integration",
      );
    next.provider = update.provider;
    changes.push({ field: "provider", change: "updated" });
  }
  if (update.enabled !== undefined && update.enabled !== next.enabled) {
    next.enabled = update.enabled;
    next.status = update.enabled ? "ok" : "disabled";
    changes.push({ field: "enabled", change: "updated" });
  }
  for (const [key, value] of Object.entries(update.fields ?? {})) {
    const field = next.fields.find((f) => f.key === key);
    if (!field || !field.editable)
      throw new ApiError(`unknown field '${key}'`, 422, "invalid_integration");
    if (value === null || same(value, field.default)) {
      if (field.overridden) changes.push({ field: key, change: "reverted" });
      Object.assign(field, { value: field.default, overridden: false });
      continue;
    }
    checkValue(key, field.type, value);
    if (!same(value, field.value))
      changes.push({ field: key, change: field.overridden ? "updated" : "set" });
    Object.assign(field, { value, overridden: true });
  }
  for (const [name, value] of Object.entries(update.secrets ?? {})) {
    if (!HEADER.test(name))
      throw new ApiError(
        `secret name '${name}' must be an HTTP header name`,
        422,
        "invalid_integration",
      );
    const existing = next.secrets.findIndex((s) => s.name === name && s.source === "ui");
    if (!value) {
      if (existing >= 0) {
        next.secrets.splice(existing, 1);
        changes.push({ field: `secrets.${name}`, change: "cleared" });
      }
      continue;
    }
    if (!secretsEnabled)
      throw new ApiError(
        `secret ${name} can't be stored: no encryption key (set AIOPS_SECRETS_KEY on the API)`,
        422,
        "secrets_key_missing",
      );
    // the value goes no further than this line: only the hint is kept
    const stored = { name, configured: true, last4: secretHint(value), source: "ui", usable: true };
    changes.push({ field: `secrets.${name}`, change: existing >= 0 ? "updated" : "set" });
    next.secrets = [
      ...next.secrets.filter((s) => s.name !== name),
      stored as Integration["secrets"][number],
    ].sort((a, b) => a.name.localeCompare(b.name));
  }
  const kept = (key: string) =>
    (!update.reset && item.overridden.includes(key)) || changes.some((c) => c.field === key);
  next.overridden = [
    ...["enabled", "provider"].filter(kept),
    ...next.fields.filter((f) => f.overridden).map((f) => f.key),
    ...next.secrets.filter((s) => s.source === "ui").map((s) => `secrets.${s.name}`),
  ];
  if (changes.length) {
    next.updated_at = now;
    next.updated_by = actor;
  }
  return { integration: next, changes, warnings: [] };
}

const SMOKE: Record<string, string> = {
  logs: "list_indices: 12 readable indices",
  metrics: "count(up): 9 targets",
  alerts: "list_alerts: 2 alerts",
  k8s: "list_deployments in prod: 5 deployments",
  code: "list_repositories: 4 repositories",
  tickets: "jira_search project OPS: 25 issues",
  knowledge: "list_docs: 9 runbooks",
};

/** A canned `aiops doctor` answer. A URL on port 9 (discard) or `.invalid` fails reachability. */
export function demoTestResult(
  item: Integration,
  draft?: IntegrationUpdate,
): IntegrationTestResult {
  const current = item.fields.find((f) => f.key === "mcp.url")?.value;
  const url = String(draft?.fields?.["mcp.url"] ?? current ?? "");
  const enabled = draft?.enabled ?? item.enabled;
  if (!item.configured || !enabled)
    return {
      capability: item.capability,
      status: "skip",
      duration_ms: 0,
      checks: [
        {
          check: "config",
          status: "skip",
          detail: `${item.capability} is ${item.configured ? "disabled" : "not configured in the profile"}`,
          hint: "enable it to test the connection",
          latency_ms: null,
        },
      ],
    };
  const config = {
    check: "config",
    status: "ok" as const,
    detail: "provider and allowlist look right",
    hint: "",
    latency_ms: null,
  };
  if (/:9\/|\.invalid\b/.test(url))
    return {
      capability: item.capability,
      status: "fail",
      duration_ms: 1012,
      checks: [
        config,
        {
          check: "reachability",
          status: "fail",
          detail: `cannot connect to ${url}: ConnectError: All connection attempts failed`,
          hint: `start the MCP server (make mcp-up) or fix capabilities.${item.capability}.mcp.url / the network path`,
          latency_ms: null,
        },
      ],
    };
  const tools = item.tool_allowlist.length;
  return {
    capability: item.capability,
    status: "ok",
    duration_ms: 184,
    checks: [
      config,
      {
        check: "reachability",
        status: "ok",
        detail: `${url || "stdio"}: ${tools + 1} tools`,
        hint: "",
        latency_ms: 38.4,
      },
      {
        check: "contract",
        status: "ok",
        detail: `all ${tools} allowlisted tools present`,
        hint: "",
        latency_ms: null,
      },
      {
        check: "smoke",
        status: "ok",
        detail: SMOKE[item.capability] ?? "read-only smoke call ok",
        hint: "",
        latency_ms: 61.2,
      },
    ],
  };
}
