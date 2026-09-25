from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aiops.agents.tickets_agent.analysis import (
    DEFAULT_TERMS,
    ServiceScope,
    TicketAnalysis,
    assess,
    jql_value,
    keyword_jql,
    load_terms,
    mentions,
    symptom_terms,
)
from aiops.mcp.tickets import Ticket

NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
PAYMENTS = ServiceScope("payment-service", ("payments",), ("payment-service",))
USERS = ServiceScope("user-service", ("identity",), ("user-service",))


def names(
    question: str, symptoms: list[str] | None = None, hints: dict[str, object] | None = None
) -> list[str]:
    return [t.name for t in symptom_terms(question, symptoms or [], hints or {}, DEFAULT_TERMS)]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Payment API is returning HTTP 500 in production", ["http_500"]),
        ("Why are orders timing out in production?", ["timeout"]),
        ("Payments are slow in production", ["latency"]),
        ("Is anything wrong with payment-service in production?", []),
        ("Orders are failing intermittently in production", []),
        ("Login and checkout requests are failing in production", []),
        ("pods OOMKilled and ImagePullBackOff", ["oom", "image_pull"]),
    ],
)
def test_symptom_terms_from_question(question: str, expected: list[str]) -> None:
    assert names(question) == expected


def test_symptom_terms_from_symptoms_and_hints() -> None:
    assert names("x", ["db_timeout_errors_up"]) == ["timeout", "database"]
    assert names("x", hints={"signals": ["cache_connection_errors"], "trace_ids": ["abc"]}) == [
        "cache"
    ]


def test_mentions_is_word_based() -> None:
    assert mentions("DB connection timeouts", "timeout")
    assert mentions("pods were OOMKilled", "oom*")
    assert not mentions("HTTP 5000", "500")
    assert not mentions("bloom filter", "oom*")


def test_jql_quoting_and_keyword_query() -> None:
    assert jql_value('a "b" \\c') == '"a \\"b\\" \\\\c"'
    latency = next(t for t in DEFAULT_TERMS if t.name == "latency")
    assert keyword_jql("OPS", [latency], NOW) == (
        'project = "OPS" AND (text ~ "slow" OR text ~ "latency") AND '
        '(statusCategory != Done OR resolved >= "2026-09-25") ORDER BY updated DESC'
    )


def test_custom_vocabulary_replaces_defaults() -> None:
    terms = load_terms({"quota": {"triggers": "quota|429", "search": ["quota"]}})
    assert [t.name for t in symptom_terms("HTTP 429 from the API", [], {}, terms)] == ["quota"]
    assert load_terms(None) is DEFAULT_TERMS


def ticket(key: str, **fields: object) -> Ticket:
    return Ticket.model_validate({"key": key, "summary": key, **fields})


def test_categories_and_signals() -> None:
    http_500 = [t for t in DEFAULT_TERMS if t.name == "http_500"]
    tickets = [
        ticket(
            "OPS-1", labels=["payment-service"], description="HTTP 500", status_category="To Do"
        ),
        ticket("OPS-2", labels=["payment-service"], description="HTTP 500", status_category="Done"),
        ticket("OPS-3", components=["identity"], description="500 errors", status_category="To Do"),
        ticket("OPS-4", labels=["payment-service"], status_category="To Do"),
        ticket("OPS-5", labels=["other"], description="500", status_category="To Do"),
        ticket("OPS-6", labels=["other"], status_category="To Do"),
    ]
    analysis = TicketAnalysis("OPS", PAYMENTS, [USERS], http_500, NOW)
    analysis.assessments = [assess(t, PAYMENTS, [USERS], http_500) for t in tickets]
    categories = {a.ticket.key: a.category for a in analysis.assessments}
    assert categories == {
        "OPS-1": "known_issue",
        "OPS-2": "similar_past",
        "OPS-3": "dependency_known_issue",
        "OPS-4": "open_on_service",
        "OPS-5": "keyword_other",
        "OPS-6": "unrelated",
    }
    assert analysis.signals == [
        "known_issue_open",
        "dependency_known_issue_open",
        "similar_past_incident",
        "related_open_tickets",
    ]
    assert [a.ticket.key for a in analysis.relevant][:3] == ["OPS-1", "OPS-3", "OPS-2"]
    assert analysis.strong


def test_no_related_tickets() -> None:
    analysis = TicketAnalysis("OPS", PAYMENTS, [], [], NOW)
    analysis.assessments = [assess(ticket("OPS-9", labels=["x"]), PAYMENTS, [], [])]
    assert analysis.signals == ["no_related_tickets"] and not analysis.strong
    assert "No related tickets found." in analysis.lines()
