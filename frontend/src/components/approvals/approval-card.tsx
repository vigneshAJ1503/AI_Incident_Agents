"use client";

import {
  CheckIcon,
  ExternalLinkIcon,
  LoaderCircleIcon,
  ShieldCheckIcon,
  XIcon,
} from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";

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
import { Textarea } from "@/components/ui/input";
import type { Approval } from "@/lib/api/schemas";
import { CURRENT_USER } from "@/lib/config";
import { formatRelative } from "@/lib/format";
import { useDecision } from "@/lib/queries";

const STATUS_TONE = {
  pending: "warn",
  approved: "ok",
  executed: "ok",
  denied: "neutral",
  failed: "danger",
} as const;

/** What will be written: the tool call arguments, rendered for review. */
export function ApprovalPreview({ approval }: { approval: Approval }) {
  const args = approval.arguments;
  const summary = typeof args.summary === "string" ? args.summary : null;
  const description = typeof args.description === "string" ? args.description : null;
  const rest = Object.entries(args).filter(([k]) => k !== "summary" && k !== "description");
  return (
    <div className="space-y-3 rounded-lg border bg-muted/30 p-3 text-sm">
      <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <ShieldCheckIcon aria-hidden className="size-3.5" />
        <span>
          <span className="font-mono text-foreground">{approval.capability}</span> ·{" "}
          <span className="font-mono text-foreground">{approval.tool}</span>
        </span>
        <Badge
          tone={
            approval.risk === "high" ? "danger" : approval.risk === "medium" ? "warn" : "neutral"
          }
        >
          risk {approval.risk}
        </Badge>
      </p>
      {summary && <p className="font-medium">{summary}</p>}
      {rest.length > 0 && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
          {rest.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-muted-foreground">{k}</dt>
              <dd className="font-mono break-words">
                {Array.isArray(v) ? v.join(", ") : String(v)}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {description && (
        <details className="group">
          <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">
            Ticket description (markdown)
          </summary>
          <pre className="mt-2 max-h-64 overflow-auto rounded-md bg-card p-2.5 font-mono text-[11.5px] whitespace-pre-wrap">
            {description}
          </pre>
        </details>
      )}
    </div>
  );
}

function resultLink(a: Approval): { key: string; url: string } | null {
  const r = a.result;
  if (r && typeof r.key === "string" && typeof r.url === "string")
    return { key: r.key, url: r.url };
  return null;
}

/** Approve/deny with a confirmation step, a toast and an optimistic update. */
export function ApprovalActions({
  approval,
  onDone,
}: {
  approval: Approval;
  onDone?: (a: Approval) => void;
}) {
  const decision = useDecision();
  const [confirm, setConfirm] = useState<"approve" | "deny" | null>(null);
  const [comment, setComment] = useState("");

  const run = async (d: "approve" | "deny") => {
    try {
      const updated = await decision.mutateAsync({
        id: approval.id,
        decision: d,
        by: CURRENT_USER,
        comment: comment || undefined,
      });
      setConfirm(null);
      const link = resultLink(updated);
      if (d === "approve") {
        toast.success(link ? `Created ${link.key}` : "Approved", {
          description: link ? "The Jira ticket was created with the RCA." : approval.action,
          action: link
            ? { label: "Open", onClick: () => window.open(link.url, "_blank", "noopener") }
            : undefined,
        });
      } else {
        toast("Denied", { description: "Nothing was written." });
      }
      onDone?.(updated);
    } catch (err) {
      toast.error("The decision failed", { description: (err as Error).message });
    }
  };

  if (approval.status !== "pending") {
    const link = resultLink(approval);
    return (
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge tone={STATUS_TONE[approval.status]}>{approval.status}</Badge>
        {approval.decided_by && (
          <span>
            by {approval.decided_by} {formatRelative(approval.decided_at)}
          </span>
        )}
        {approval.comment && <span>“{approval.comment}”</span>}
        {link && (
          <a
            className="inline-flex items-center gap-1 text-primary hover:underline"
            href={link.url}
            target="_blank"
            rel="noopener noreferrer"
          >
            {link.key} <ExternalLinkIcon aria-hidden className="size-3" />
          </a>
        )}
      </div>
    );
  }

  return (
    <>
      <div className="flex gap-2">
        <Button
          size="sm"
          onClick={() => setConfirm("approve")}
          data-testid={`approve-${approval.id}`}
        >
          <CheckIcon /> Approve
        </Button>
        <Button size="sm" variant="outline" onClick={() => setConfirm("deny")}>
          <XIcon /> Deny
        </Button>
      </div>
      <Dialog open={confirm !== null} onOpenChange={(o) => !o && setConfirm(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {confirm === "approve" ? "Approve and execute?" : "Deny this proposal?"}
            </DialogTitle>
            <DialogDescription>
              {confirm === "approve"
                ? `This runs ${approval.tool} on ${approval.capability}. The decision is recorded in the audit log.`
                : "Nothing will be written. The decision is recorded in the audit log."}
            </DialogDescription>
          </DialogHeader>
          <ApprovalPreview approval={approval} />
          <label className="space-y-1 text-sm">
            <span className="text-xs text-muted-foreground">Comment (optional)</span>
            <Textarea value={comment} onChange={(e) => setComment(e.target.value)} rows={2} />
          </label>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirm(null)}>
              Cancel
            </Button>
            <Button
              variant={confirm === "deny" ? "destructive" : "default"}
              disabled={decision.isPending}
              onClick={() => confirm && void run(confirm)}
              data-testid="confirm-decision"
            >
              {decision.isPending && <LoaderCircleIcon className="animate-spin" />}
              {confirm === "approve" ? "Approve" : "Deny"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export function ApprovalCard({ approval }: { approval: Approval }) {
  return (
    <article
      className="space-y-3 rounded-xl border bg-card p-5 shadow-xs"
      data-testid="approval-card"
      aria-label={approval.action}
    >
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="font-medium">{approval.action.replace(/_/g, " ")}</h2>
          <p className="text-xs text-muted-foreground">
            requested by {approval.requested_by} {formatRelative(approval.created_at)}
            {approval.investigation_id && (
              <>
                {" · "}
                <Link
                  className="text-primary underline underline-offset-2"
                  href={`/investigations/${approval.investigation_id}` as Route}
                >
                  {approval.investigation_id}
                </Link>
              </>
            )}
          </p>
        </div>
        <Badge tone={STATUS_TONE[approval.status]}>{approval.status}</Badge>
      </header>
      <p className="text-sm text-muted-foreground">{approval.reason}</p>
      <ApprovalPreview approval={approval} />
      <ApprovalActions approval={approval} />
    </article>
  );
}
