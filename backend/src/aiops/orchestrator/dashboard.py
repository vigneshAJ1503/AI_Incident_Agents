"""Dashboard summary (docs/api/contract.md "Dashboard"), computed from investigations.

Pure functions over ``Investigation`` objects, so the API (PR-035) and the demo export
share them.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from aiops.core.models import AgentStatus, Investigation, InvestigationStatus
from aiops.core.signals import BENIGN_SIGNALS
from aiops.store.repository import summary_of

OPEN = {
    InvestigationStatus.PENDING,
    InvestigationStatus.RUNNING,
    InvestigationStatus.NEEDS_CLARIFICATION,
}
SEVERITIES = ("critical", "high", "medium", "low")


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[index], 1)


def root_cause_label(inv: Investigation) -> str | None:
    report = inv.report
    if report is None or report.root_cause_hypothesis_id is None:
        return None
    top = next((h for h in inv.hypotheses if h.id == report.root_cause_hypothesis_id), None)
    if top is None:
        return None
    for separator in (" introduced ", " causes ", " forces ", " references ", " correlates "):
        if separator in top.statement:
            return top.statement.split(separator)[0].strip()[:80]
    return top.statement[:80]


def agent_stats(investigations: Sequence[Investigation]) -> list[dict[str, Any]]:
    runs: dict[str, list[tuple[bool, float, int]]] = {}
    last: dict[str, datetime] = {}
    for inv in investigations:
        for result in inv.results:
            runs.setdefault(result.agent, []).append(
                (
                    result.status is not AgentStatus.FAILED,
                    result.duration_ms,
                    result.usage.total_tokens,
                )
            )
            if inv.completed_at and (
                result.agent not in last or inv.completed_at > last[result.agent]
            ):
                last[result.agent] = inv.completed_at
    stats = []
    for name in sorted(runs):
        items = runs[name]
        stats.append(
            {
                "name": name,
                "runs": len(items),
                "success_rate": round(sum(ok for ok, _, _ in items) / len(items), 3),
                "p50_ms": round(statistics.median(d for _, d, _ in items), 1),
                "tokens": sum(t for _, _, t in items),
                "last_run_at": last[name].isoformat() if name in last else None,
            }
        )
    return stats


def _top_root_cause(items: Sequence[Investigation]) -> str | None:
    labels = Counter(label for i in items if (label := root_cause_label(i)))
    return labels.most_common(1)[0][0] if labels else None


def dashboard_summary(
    investigations: Sequence[Investigation], *, days: int = 14, now: datetime
) -> dict[str, Any]:
    since = now - timedelta(days=days)
    window = sorted(
        (i for i in investigations if i.created_at >= since),
        key=lambda i: i.created_at,
        reverse=True,
    )
    finished = [i for i in window if i.report is not None and i.duration_ms is not None]
    with_root = [i for i in window if i.report and i.report.root_cause_hypothesis_id]
    by_day: dict[str, Counter[str]] = {}
    for offset in range(days - 1, -1, -1):
        by_day[(now - timedelta(days=offset)).date().isoformat()] = Counter()
    for inv in window:
        day = by_day.get(inv.created_at.date().isoformat())
        if day is not None:
            day["investigations"] += 1
            if inv.report and inv.report.severity in SEVERITIES:
                day[inv.report.severity] += 1
    services: dict[str, list[Investigation]] = {}
    for inv in window:
        service = (inv.context.service if inv.context else None) or inv.incident.service
        if service:
            services.setdefault(service, []).append(inv)
    signals: Counter[str] = Counter()
    for inv in window:
        seen = {s for r in inv.results for s in r.signals if s not in BENIGN_SIGNALS}
        signals.update(seen)
    return {
        "window_days": days,
        "totals": {
            "investigations": len(window),
            "open": sum(i.status in OPEN for i in window),
            "root_cause_found": len(with_root),
            "no_incident": sum(
                1
                for i in window
                if i.status is InvestigationStatus.COMPLETED
                and i.report is not None
                and i.report.severity == "none"
            ),
            "failed": sum(i.status is InvestigationStatus.FAILED for i in window),
        },
        "mttr_minutes": {
            "p50": _percentile([(i.duration_ms or 0) / 60000 for i in finished], 0.5),
            "p90": _percentile([(i.duration_ms or 0) / 60000 for i in finished], 0.9),
        },
        "avg_confidence": round(
            statistics.mean(i.report.confidence for i in with_root if i.report), 2
        )
        if with_root
        else 0.0,
        "by_day": [
            {"date": day, "investigations": c["investigations"], **{s: c[s] for s in SEVERITIES}}
            for day, c in by_day.items()
        ],
        "by_service": [
            {
                "service": name,
                "investigations": len(items),
                "top_root_cause": _top_root_cause(items),
            }
            for name, items in sorted(services.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ],
        "top_signals": [{"signal": s, "count": c} for s, c in signals.most_common(10)],
        "agents": [{k: v for k, v in a.items() if k != "last_run_at"} for a in agent_stats(window)],
        "recent": [summary_of(i) for i in window[:5]],
    }
