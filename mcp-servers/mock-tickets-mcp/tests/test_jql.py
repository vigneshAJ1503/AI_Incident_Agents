from __future__ import annotations

import pytest

from mock_tickets_mcp.jql import JQLError, parse, search, text_matches
from tests.sample import NOW, issues


def keys(jql: str) -> list[str]:
    return [i.key for i in search(issues(), parse(jql), NOW)]


@pytest.mark.parametrize(
    ("jql", "expected"),
    [
        ("project = OPS ORDER BY key ASC", ["OPS-2", "OPS-3", "OPS-7", "OPS-12"]),
        (
            "project in (OPS, WEB) AND labels = payment-service ORDER BY key",
            ["OPS-2", "OPS-12", "WEB-1"],
        ),
        ('status = "In Progress"', ["OPS-7"]),
        ("statusCategory != Done ORDER BY created ASC", ["OPS-7", "OPS-12", "WEB-1"]),
        ("component in (payments, orders) ORDER BY key", ["OPS-2", "OPS-3", "OPS-12"]),
        ('text ~ "timeout" ORDER BY key', ["OPS-2", "OPS-7", "OPS-12", "WEB-1"]),
        ('summary ~ "timeouts" AND project = OPS', ["OPS-12"]),
        ('text ~ "index migration"', ["OPS-7"]),  # comments are searchable
        ('text ~ "oom*"', ["OPS-3"]),
        ('resolved >= "2026-06-27" ORDER BY key', ["OPS-3"]),
        ("created >= -5d AND project = OPS", ["OPS-12"]),
        (
            "resolution = Unresolved AND project = OPS ORDER BY priority DESC, key ASC",
            ["OPS-7", "OPS-12"],
        ),
        ("resolution is EMPTY AND project = OPS ORDER BY key", ["OPS-7", "OPS-12"]),
        ("NOT labels = payment-service AND project = OPS ORDER BY key", ["OPS-3", "OPS-7"]),
        (
            "project = OPS AND (labels in (order-service) OR text ~ 500) ORDER BY key",
            ["OPS-3", "OPS-12"],
        ),
        ("issuetype = Incident ORDER BY resolved DESC", ["OPS-3", "OPS-2"]),
        ("ORDER BY updated DESC", ["OPS-7", "OPS-12", "WEB-1", "OPS-3", "OPS-2"]),
        ("labels not in (payment-service, oom) ORDER BY key", ["OPS-7"]),
    ],
)
def test_supported_subset(jql: str, expected: list[str]) -> None:
    assert keys(jql) == expected


@pytest.mark.parametrize(
    ("jql", "message"),
    [
        ("assignee = currentUser()", "unsupported JQL field 'assignee'"),
        ("project = OPS AND updated >= startOfDay()", "functions are not supported"),
        ("status WAS Open", "history operators are not supported"),
        ("cf[10010] = 3", "unsupported JQL field"),
        ("labels ~ foo", "operator '~' is not supported for field 'labels'"),
        ("text = foo", "operator '=' is not supported for field 'text'"),
        ("project = OPS AND", "expected a field name"),
        ("project = OPS ORDER BY summary", "cannot ORDER BY 'summary'"),
        ("(project = OPS", "expected )"),
        ("project = OPS extra", "unexpected 'extra'"),
        ('created >= "yesterday"', "invalid date"),
        ("labels in (a, b", "expected )"),
    ],
)
def test_rejects_unsupported(jql: str, message: str) -> None:
    with pytest.raises(JQLError, match=message.replace("(", r"\(").replace(")", r"\)")):
        search(issues(), parse(jql), NOW)


def test_text_matching_is_word_based_and_stemmed() -> None:
    assert text_matches("DB connection timeouts", "timeout")
    assert text_matches("slow queries", "query")
    assert text_matches("HTTP 500 errors", "http 500")
    assert not text_matches("HTTP 5000 errors", "500")
    assert not text_matches("connection pool", "timeout")
    assert text_matches("OOMKilled", "oom*")
    assert not text_matches("anything", "")
