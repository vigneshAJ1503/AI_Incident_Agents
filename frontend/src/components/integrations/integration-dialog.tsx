"use client";

import {
  CircleCheckIcon,
  CircleMinusIcon,
  CircleXIcon,
  KeyRoundIcon,
  LoaderCircleIcon,
  PlugZapIcon,
  RotateCcwIcon,
  SaveIcon,
  TriangleAlertIcon,
  type LucideIcon,
} from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";

import { IntegrationStatusBadge } from "@/components/integrations/integration-card";
import { describeError } from "@/components/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input, NativeSelect } from "@/components/ui/input";
import type {
  CheckStatus,
  FieldValue,
  Integration,
  IntegrationField,
  IntegrationTestResult,
} from "@/lib/api/schemas";
import {
  HEADER_NAME,
  buildUpdate,
  capabilityTitle,
  fieldGroup,
  fieldLabel,
  fromInput,
  initialForm,
  maskedSecret,
  toInput,
  type FieldGroup,
  type FormState,
} from "@/lib/integrations";
import { useSaveIntegration, useTestIntegration } from "@/lib/queries";
import { cn } from "@/lib/utils";

const CHECK_META: Record<CheckStatus, { icon: LucideIcon; cls: string; label: string }> = {
  ok: { icon: CircleCheckIcon, cls: "text-ok", label: "pass" },
  warn: { icon: TriangleAlertIcon, cls: "text-warn", label: "warn" },
  fail: { icon: CircleXIcon, cls: "text-danger", label: "fail" },
  skip: { icon: CircleMinusIcon, cls: "text-muted-foreground", label: "skipped" },
};

const GROUPS: { id: FieldGroup; title: string }[] = [
  { id: "connection", title: "Connection" },
  { id: "settings", title: "Provider settings" },
  { id: "limits", title: "Limits" },
];

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: IntegrationField;
  value: unknown;
  onChange: (value: FieldValue) => void;
}) {
  const id = useId();
  // the raw text, so a number/list can be cleared and retyped (the parsed value goes up)
  const [text, setText] = useState(() => toInput(field, value));
  const label = fieldLabel(field.key);
  const changed = field.overridden;
  const hint =
    field.default === null || field.default === undefined || field.default === ""
      ? "empty in the profile"
      : `profile: ${toInput(field, field.default)}`;
  return (
    <div className="grid gap-1">
      <div className="flex items-center gap-2">
        <label htmlFor={id} className="font-mono text-xs font-medium">
          {label}
        </label>
        {changed && (
          <Badge tone="purple" className="px-1.5 py-0 text-[10px]">
            UI override
          </Badge>
        )}
      </div>
      {field.type === "boolean" ? (
        <label className="flex items-center gap-2 text-sm">
          <input
            id={id}
            type="checkbox"
            className="size-4 accent-(--primary)"
            checked={Boolean(value)}
            disabled={!field.editable}
            onChange={(e) => onChange(e.target.checked)}
          />
          <span className="text-muted-foreground">{value ? "on" : "off"}</span>
        </label>
      ) : (
        <Input
          id={id}
          className="font-mono text-xs"
          type={field.type === "number" ? "number" : "text"}
          step="any"
          value={text}
          disabled={!field.editable}
          spellCheck={false}
          autoComplete="off"
          aria-describedby={`${id}-hint`}
          onChange={(e) => {
            setText(e.target.value);
            onChange(fromInput(field, e.target.value));
          }}
        />
      )}
      <p id={`${id}-hint`} className="truncate text-[11px] text-muted-foreground" title={hint}>
        {field.editable ? hint : "set in profile.yaml (guardrail or structured setting)"}
      </p>
    </div>
  );
}

function TestResults({ result }: { result: IntegrationTestResult }) {
  const overall = CHECK_META[result.status];
  return (
    <section
      aria-label="Test results"
      data-testid="test-results"
      className="max-h-48 overflow-y-auto rounded-lg border border-glass-border bg-glass p-3"
    >
      <p className={cn("mb-2 flex items-center gap-2 text-sm font-medium", overall.cls)}>
        <overall.icon aria-hidden className="size-4" />
        Test {overall.label} · {Math.round(result.duration_ms)} ms
      </p>
      <ul className="space-y-1.5 text-xs">
        {result.checks.map((c, i) => {
          const meta = CHECK_META[c.status];
          return (
            <li key={`${c.check}-${i}`} className="flex gap-2">
              <meta.icon aria-hidden className={cn("mt-0.5 size-3.5 shrink-0", meta.cls)} />
              <div className="min-w-0">
                <span className="font-mono font-medium">{c.check}</span>{" "}
                <span className="sr-only">{meta.label}: </span>
                <span className="break-words text-muted-foreground">{c.detail}</span>
                {c.latency_ms != null && (
                  <span className="text-muted-foreground"> ({Math.round(c.latency_ms)} ms)</span>
                )}
                {c.hint && c.status !== "ok" && (
                  <p className="text-muted-foreground italic">Fix: {c.hint}</p>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function SecretsSection({
  item,
  form,
  setForm,
  secretsEnabled,
}: {
  item: Integration;
  form: FormState;
  setForm: (f: FormState) => void;
  secretsEnabled: boolean;
}) {
  const nameId = useId();
  const valueId = useId();
  const [name, setName] = useState("Authorization");
  const [value, setValue] = useState("");
  const pending = Object.entries(form.secrets);
  const stage = () => {
    if (!HEADER_NAME.test(name) || !value) return;
    setForm({ ...form, secrets: { ...form.secrets, [name]: value } });
    setValue("");
  };
  if (item.transport !== "http") return null;
  return (
    <fieldset className="grid gap-2">
      <legend className="mb-1 text-sm font-semibold">Secrets</legend>
      <p className="text-xs text-muted-foreground">
        HTTP headers sent to the MCP server (e.g. a bearer token). Write-only: once saved, only the
        last 4 characters are ever shown.
      </p>
      <ul className="flex flex-wrap gap-1.5">
        {item.secrets.map((s) => {
          const clearing = form.secrets[s.name] === null;
          return (
            <li key={s.name}>
              <Badge tone={clearing ? "warn" : s.usable ? "neutral" : "warn"} className="font-mono">
                <KeyRoundIcon aria-hidden />
                {s.name}: {clearing ? "will be cleared" : maskedSecret(s)}
                {!s.usable && " (unreadable: save again)"}
                {s.source === "ui" && !clearing && (
                  <button
                    type="button"
                    className="ml-1 underline-offset-2 hover:underline"
                    onClick={() =>
                      setForm({ ...form, secrets: { ...form.secrets, [s.name]: null } })
                    }
                  >
                    Clear<span className="sr-only"> {s.name}</span>
                  </button>
                )}
              </Badge>
            </li>
          );
        })}
        {pending
          .filter(([, v]) => v)
          .map(([n]) => (
            <li key={`new-${n}`}>
              <Badge tone="info" className="font-mono">
                <KeyRoundIcon aria-hidden />
                {n}: new value (unsaved)
              </Badge>
            </li>
          ))}
      </ul>
      {secretsEnabled ? (
        <div className="grid gap-2 sm:grid-cols-[10rem_1fr_auto] sm:items-end">
          <div className="grid gap-1">
            <label htmlFor={nameId} className="text-xs font-medium">
              Header
            </label>
            <Input
              id={nameId}
              className="font-mono text-xs"
              value={name}
              autoComplete="off"
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="grid gap-1">
            <label htmlFor={valueId} className="text-xs font-medium">
              Secret value
            </label>
            <Input
              id={valueId}
              type="password"
              className="font-mono text-xs"
              value={value}
              autoComplete="new-password"
              placeholder="Bearer …"
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  stage();
                }
              }}
            />
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-9"
            disabled={!HEADER_NAME.test(name) || !value}
            onClick={stage}
          >
            Set secret
          </Button>
        </div>
      ) : (
        <p className="rounded-md bg-warn-bg px-2 py-1.5 text-xs text-warn" role="note">
          Saving secrets is off: set <code className="font-mono">AIOPS_SECRETS_KEY</code> on the API
          (a Fernet key), or keep the secret in the profile&apos;s .env as a ${"{VAR}"} reference.
        </p>
      )}
    </fieldset>
  );
}

export function IntegrationDialog({
  item,
  secretsEnabled,
  open,
  onOpenChange,
}: {
  item: Integration | null;
  secretsEnabled: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {item && (
        <DialogContent className="max-w-2xl" data-testid="integration-dialog">
          {/* keyed: a fresh form every time the dialog opens on an integration */}
          <IntegrationForm
            key={`${item.capability}-${item.updated_at ?? ""}`}
            item={item}
            secretsEnabled={secretsEnabled}
            onDone={() => onOpenChange(false)}
          />
        </DialogContent>
      )}
    </Dialog>
  );
}

function IntegrationForm({
  item,
  secretsEnabled,
  onDone,
}: {
  item: Integration;
  secretsEnabled: boolean;
  onDone: () => void;
}) {
  const [form, setForm] = useState<FormState>(() => initialForm(item));
  const save = useSaveIntegration();
  const test = useTestIntegration();
  const providerId = useId();
  const enabledId = useId();
  const title = capabilityTitle(item.capability);
  const update = buildUpdate(item, form);
  const value = (f: IntegrationField) =>
    f.key in form.fields ? (form.fields[f.key] ?? f.default) : f.value;
  const setField = (key: string, v: FieldValue) =>
    setForm({ ...form, fields: { ...form.fields, [key]: v } });

  const onSave = () => {
    if (!update) return;
    save.mutate(
      { capability: item.capability, update },
      {
        onSuccess: (saved) => {
          const n = saved.changes.length;
          toast.success(`${title} saved`, {
            description: n
              ? `${n} change${n > 1 ? "s" : ""}; applies to new investigations (no restart).`
              : "Nothing changed.",
          });
          for (const w of saved.warnings) toast.warning(w);
          onDone();
        },
      },
    );
  };
  const onReset = () =>
    save.mutate(
      { capability: item.capability, update: { reset: true } },
      {
        onSuccess: () => {
          toast.success(`${title} reset to the profile`);
          onDone();
        },
      },
    );

  return (
    <>
      <DialogHeader>
        <DialogTitle className="flex flex-wrap items-center gap-2">
          Configure {title}
          <IntegrationStatusBadge status={item.status} />
        </DialogTitle>
        <DialogDescription>
          Saved as an override of the profile&apos;s YAML (the source of defaults), validated like{" "}
          <code className="font-mono text-xs">aiops profile validate</code>, audited, and used by
          new investigations right away.
        </DialogDescription>
      </DialogHeader>

      <form
        className="grid gap-5"
        onSubmit={(e) => {
          e.preventDefault();
          onSave();
        }}
      >
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="grid gap-1">
            <label htmlFor={providerId} className="text-xs font-medium">
              Provider
            </label>
            <NativeSelect
              id={providerId}
              value={form.provider ?? ""}
              onChange={(e) => setForm({ ...form, provider: e.target.value })}
            >
              {item.providers.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </NativeSelect>
          </div>
          <label htmlFor={enabledId} className="flex items-center gap-2 self-end pb-2 text-sm">
            <input
              id={enabledId}
              type="checkbox"
              className="size-4 accent-(--primary)"
              checked={form.enabled}
              onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
            />
            Enabled (agents use this capability)
          </label>
        </div>

        {GROUPS.map((g) => {
          const fields = item.fields.filter((f) => fieldGroup(f.key) === g.id);
          if (!fields.length) return null;
          return (
            <fieldset key={g.id} className="grid gap-3">
              <legend className="mb-1 text-sm font-semibold">{g.title}</legend>
              <div className="grid gap-3 sm:grid-cols-2">
                {fields.map((f) => (
                  <FieldInput
                    key={f.key}
                    field={f}
                    value={value(f)}
                    onChange={(v) => setField(f.key, v)}
                  />
                ))}
              </div>
            </fieldset>
          );
        })}

        <SecretsSection item={item} form={form} setForm={setForm} secretsEnabled={secretsEnabled} />

        <div className="grid gap-1">
          <p className="text-sm font-semibold">Read-only tool allowlist</p>
          <p className="flex flex-wrap gap-1">
            {item.tool_allowlist.map((t) => (
              <Badge key={t} tone="outline" className="font-mono">
                {t}
              </Badge>
            ))}
          </p>
          <p className="text-[11px] text-muted-foreground">
            The agents&apos; security boundary: change it in the reviewed profile.yaml.
          </p>
        </div>

        {/* sticky: the actions and their results stay in view however long the form is */}
        <div className="sticky -bottom-6 -mx-6 -mb-6 grid gap-3 border-t border-glass-border bg-popover px-6 py-4">
          {test.data && <TestResults result={test.data} />}
          {(save.error ?? test.error) && (
            <p role="alert" className="rounded-md bg-danger-bg px-3 py-2 text-sm text-danger">
              {describeError(save.error ?? test.error).detail}
            </p>
          )}
          <DialogFooter className="items-center">
            {item.overridden.length > 0 && (
              <Button
                type="button"
                variant="ghost"
                className="sm:mr-auto"
                disabled={save.isPending}
                onClick={onReset}
              >
                <RotateCcwIcon aria-hidden />
                Reset to profile
              </Button>
            )}
            <Button
              type="button"
              variant="outline"
              disabled={test.isPending}
              onClick={() =>
                test.mutate({ capability: item.capability, draft: update ?? undefined })
              }
            >
              {test.isPending ? (
                <LoaderCircleIcon aria-hidden className="animate-spin motion-reduce:animate-none" />
              ) : (
                <PlugZapIcon aria-hidden />
              )}
              Test connection
            </Button>
            <Button type="submit" disabled={!update || save.isPending}>
              {save.isPending ? (
                <LoaderCircleIcon aria-hidden className="animate-spin motion-reduce:animate-none" />
              ) : (
                <SaveIcon aria-hidden />
              )}
              Save
            </Button>
          </DialogFooter>
        </div>
      </form>
    </>
  );
}
