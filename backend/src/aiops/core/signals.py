"""Shared signal vocabulary."""

from __future__ import annotations

#: Signals that don't claim a problem (a healthy scenario may report them).
BENIGN_SIGNALS = frozenset(
    {
        "no_errors",
        # tickets: context only (open tickets exist on the service / nothing related found)
        "related_open_tickets",
        "no_related_tickets",
        # knowledge: no runbook matched (nothing claimed)
        "no_relevant_docs",
        # alerts: nothing firing (the explicit "all clear")
        "no_active_alerts",
        # k8s: the workload itself looks fine
        "healthy",
        # code: no risky change in the lookback window
        "no_recent_changes",
        # metrics: every metric within its baseline
        "no_anomaly",
    }
)
