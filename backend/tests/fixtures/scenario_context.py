"""Fixed-time task context shared by fixture recording and replay tests."""

from __future__ import annotations

from datetime import UTC, datetime

from aiops.core.models import AgentTask, IncidentContext, TimeRange

FIXED_NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
QUESTIONS = {
    "S0": "Is anything wrong with payment-service in production?",
    "S1": "Payment API is returning HTTP 500 in production",
}


def task_for(scenario: str, service: str = "payment-service") -> AgentTask:
    context = IncidentContext(
        question=QUESTIONS[scenario],
        service=service,
        environment="production",
        time_range=TimeRange.last("30m", now=FIXED_NOW),
    )
    return AgentTask(agent="logs", objective=context.question, context=context)
