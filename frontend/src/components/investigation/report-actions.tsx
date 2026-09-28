"use client";

import { useQueryClient } from "@tanstack/react-query";
import { CopyIcon, DownloadIcon, LoaderCircleIcon, RotateCcwIcon, TicketIcon } from "lucide-react";
import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { ApprovalActions, ApprovalPreview } from "@/components/approvals/approval-card";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { getClient } from "@/lib/api";
import type { Approval, Investigation } from "@/lib/api/schemas";
import { useCreateInvestigation } from "@/lib/queries";

export function ReportActions({ inv }: { inv: Investigation }) {
  const router = useRouter();
  const qc = useQueryClient();
  const create = useCreateInvestigation();
  const [draft, setDraft] = useState<Approval | null>(null);
  const [drafting, setDrafting] = useState(false);

  const markdown = async () => inv.report?.markdown || (await getClient().reportMarkdown(inv.id));

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(await markdown());
      toast.success("Report copied as Markdown");
    } catch (err) {
      toast.error("Copy failed", { description: (err as Error).message });
    }
  };

  const download = async () => {
    const blob = new Blob([await markdown()], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${inv.incident.id}-${inv.id}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const jira = async () => {
    setDrafting(true);
    try {
      const { approval_id } = await getClient().draftTicket(inv.id);
      const list = await getClient().approvals();
      void qc.invalidateQueries({ queryKey: ["approvals"] });
      const a = list.find((x) => x.id === approval_id);
      if (!a) throw new Error(`Approval ${approval_id} not found`);
      setDraft(a);
    } catch (err) {
      toast.error("Could not draft the ticket", { description: (err as Error).message });
    } finally {
      setDrafting(false);
    }
  };

  const rerun = async () => {
    try {
      const res = await create.mutateAsync({
        question: inv.incident.title,
        ...(inv.incident.service ? { service: inv.incident.service } : {}),
        ...(inv.incident.environment ? { environment: inv.incident.environment } : {}),
      });
      router.push(`/investigations/${res.id}` as Route);
    } catch (err) {
      toast.error("Could not re-run", { description: (err as Error).message });
    }
  };

  return (
    <div className="flex flex-wrap gap-2">
      <Button variant="outline" size="sm" onClick={() => void copy()}>
        <CopyIcon /> Copy report
      </Button>
      <Button variant="outline" size="sm" onClick={() => void download()}>
        <DownloadIcon /> Download .md
      </Button>
      <Button variant="outline" size="sm" onClick={() => void rerun()} disabled={create.isPending}>
        <RotateCcwIcon /> Re-run
      </Button>
      <Button size="sm" onClick={() => void jira()} disabled={drafting} data-testid="create-jira">
        {drafting ? <LoaderCircleIcon className="animate-spin" /> : <TicketIcon />} Create Jira
        ticket
      </Button>
      <Dialog open={draft !== null} onOpenChange={(o) => !o && setDraft(null)}>
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>Create a Jira ticket</DialogTitle>
            <DialogDescription>
              Write actions need an explicit approval (UC-04). Review the draft; approving creates
              the ticket.
            </DialogDescription>
          </DialogHeader>
          {draft && (
            <>
              <ApprovalPreview approval={draft} />
              <ApprovalActions
                approval={draft}
                onDone={(a) => setDraft(a.status === "pending" ? a : null)}
              />
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
