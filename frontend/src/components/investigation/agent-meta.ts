import {
  BellRingIcon,
  BookOpenIcon,
  BoxesIcon,
  ChartLineIcon,
  GitCommitHorizontalIcon,
  ScrollTextIcon,
  SparklesIcon,
  TicketIcon,
  BotIcon,
  type LucideIcon,
} from "lucide-react";

import type { EvidenceKind } from "@/lib/api/schemas";

export const AGENT_META: Record<string, { label: string; icon: LucideIcon; tool: string }> = {
  logs: { label: "Logs agent", icon: ScrollTextIcon, tool: "Elasticsearch" },
  metrics: { label: "Metrics agent", icon: ChartLineIcon, tool: "Prometheus" },
  alerts: { label: "Alerts agent", icon: BellRingIcon, tool: "Alertmanager" },
  k8s: { label: "Kubernetes agent", icon: BoxesIcon, tool: "Kubernetes" },
  code: { label: "Code agent", icon: GitCommitHorizontalIcon, tool: "Git" },
  tickets: { label: "Tickets agent", icon: TicketIcon, tool: "Jira" },
  knowledge: { label: "Knowledge agent", icon: BookOpenIcon, tool: "Runbooks" },
  rca: { label: "RCA agent", icon: SparklesIcon, tool: "LLM" },
};

export const agentMeta = (name: string) =>
  AGENT_META[name] ?? { label: `${name} agent`, icon: BotIcon, tool: name };

export const EVIDENCE_META: Record<EvidenceKind, { label: string; icon: LucideIcon; tab: string }> =
  {
    log: { label: "Log", icon: ScrollTextIcon, tab: "logs" },
    metric: { label: "Metric", icon: ChartLineIcon, tab: "metrics" },
    alert: { label: "Alert", icon: BellRingIcon, tab: "alerts" },
    k8s_event: { label: "Kubernetes", icon: BoxesIcon, tab: "k8s" },
    commit: { label: "Commit", icon: GitCommitHorizontalIcon, tab: "changes" },
    ticket: { label: "Ticket", icon: TicketIcon, tab: "tickets" },
    doc: { label: "Runbook", icon: BookOpenIcon, tab: "runbooks" },
  };
