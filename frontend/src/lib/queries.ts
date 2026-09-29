"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getClient, type Decision, type InvestigationFilters } from "@/lib/api";
import type {
  Approval,
  AskRequest,
  CreateInvestigationRequest,
  InvestigationSummary,
} from "@/lib/api/schemas";

export const qk = {
  health: ["health"] as const,
  dashboard: (days: number) => ["dashboard", days] as const,
  investigations: (f: InvestigationFilters) => ["investigations", f] as const,
  investigation: (id: string) => ["investigation", id] as const,
  services: ["services"] as const,
  agents: ["agents"] as const,
  approvals: (status?: string) => ["approvals", status ?? "all"] as const,
  scenarios: ["scenarios"] as const,
  faultStatus: ["scenarios", "status"] as const,
};

export const useHealth = () =>
  useQuery({
    queryKey: qk.health,
    queryFn: () => getClient().health(),
    refetchInterval: 30_000,
    retry: 1,
  });

export const useDashboard = (days = 14) =>
  useQuery({
    queryKey: qk.dashboard(days),
    queryFn: () => getClient().dashboard(days),
    refetchInterval: 60_000,
  });

export const useInvestigations = (f: InvestigationFilters) =>
  useQuery({
    queryKey: qk.investigations(f),
    queryFn: () => getClient().listInvestigations(f),
    placeholderData: keepPreviousData,
  });

export const useInvestigation = (id: string) =>
  useQuery({ queryKey: qk.investigation(id), queryFn: () => getClient().getInvestigation(id) });

/**
 * The list/dashboard summary of an investigation that is already in the cache, so the detail page
 * can show (and morph) its title while the full investigation loads.
 */
export function useCachedSummary(id: string): InvestigationSummary | undefined {
  const qc = useQueryClient();
  for (const [, data] of qc.getQueriesData<{ items?: InvestigationSummary[] }>({
    queryKey: ["investigations"],
  })) {
    const hit = data?.items?.find((i) => i.id === id);
    if (hit) return hit;
  }
  for (const [, data] of qc.getQueriesData<{ recent?: InvestigationSummary[] }>({
    queryKey: ["dashboard"],
  })) {
    const hit = data?.recent?.find((i) => i.id === id);
    if (hit) return hit;
  }
  return undefined;
}

export const useServices = () =>
  useQuery({ queryKey: qk.services, queryFn: () => getClient().services(), staleTime: 300_000 });
export const useAgents = () =>
  useQuery({ queryKey: qk.agents, queryFn: () => getClient().agents(), staleTime: 60_000 });
export const useApprovals = (status?: string) =>
  useQuery({
    queryKey: qk.approvals(status),
    queryFn: () => getClient().approvals(status),
    refetchInterval: 20_000,
  });
export const useScenarios = (pollMs: number | false = false) =>
  useQuery({
    queryKey: qk.scenarios,
    queryFn: () => getClient().scenarios(),
    refetchInterval: pollMs,
  });
/** Fault injection progress; polls while a background revert runs (also after a reload). */
export const useFaultStatus = (enabled: boolean) =>
  useQuery({
    queryKey: qk.faultStatus,
    queryFn: () => getClient().scenarioStatus(),
    enabled,
    refetchInterval: (q) => (q.state.data?.reverting ? 2_000 : false),
  });

export function useCreateInvestigation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (req: CreateInvestigationRequest) => getClient().createInvestigation(req),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["investigations"] }),
  });
}

/** The chat box (PR-041): a platform answer, or a started investigation. */
export function useAsk() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (req: AskRequest) => getClient().ask(req),
    onSuccess: (res) => {
      if (res.investigation_id) void qc.invalidateQueries({ queryKey: ["investigations"] });
    },
  });
}

/** Approve/deny with an optimistic status update; rolled back on error. */
export function useDecision() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      decision,
      by,
      comment,
    }: {
      id: string;
      decision: Decision;
      by: string;
      comment?: string;
    }) => getClient().decide(id, decision, { by, comment }),
    onMutate: async ({ id, decision }) => {
      await qc.cancelQueries({ queryKey: ["approvals"] });
      const snapshots = qc.getQueriesData<Approval[]>({ queryKey: ["approvals"] });
      const next = decision === "approve" ? "approved" : "denied";
      qc.setQueriesData<Approval[]>({ queryKey: ["approvals"] }, (old) =>
        old?.map((a) => (a.id === id ? { ...a, status: next } : a)),
      );
      return { snapshots };
    },
    onError: (_err, _vars, ctx) => {
      for (const [key, data] of ctx?.snapshots ?? []) qc.setQueryData(key, data);
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: ["approvals"] }),
  });
}
