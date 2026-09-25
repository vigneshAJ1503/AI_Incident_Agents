from __future__ import annotations

from typing import Any

from aiops.agents.knowledge_agent.analysis import (
    DocMatch,
    KnowledgeAnalysis,
    QueryPlan,
    RunbookFinding,
    Search,
    build_queries,
    derive_signals,
    extract_sections,
    fuse,
    is_relevant,
    pattern_matches,
    service_words,
)

RUNBOOK = """# Redis cache outage

## Symptoms
- Log pattern: `Redis connection refused: redis:6379 (ECONNREFUSED)`.

## Diagnosis
### 1. Confirm Redis is down
`redis-cli ping`

## Mitigation
1. Restart Redis.

## Escalation
- `#platform-oncall`
"""


def test_queries_from_hints_strip_placeholders_and_expand_alerts() -> None:
    plan = build_queries(
        "Payments are slow in production",
        ["HTTP 503 on /pay"],
        {
            "signals": ["cache_connection_errors", "unknown_signal"],
            "patterns": ["Redis connection refused: redis:<NUM> (ECONNREFUSED)"],
            "alerts": ["RedisDown"],
        },
        service_words=service_words(["payment-service", "payments"]),
    )
    # placeholders dropped, words deduplicated (case-insensitive), alert name + its words,
    # then the signal vocabulary; unknown signals add nothing
    assert (
        plan.symptom_query
        == "Redis connection refused ECONNREFUSED HTTP 503 on pay RedisDown Down cache"
    )
    assert plan.question_query == "slow latency"  # service words / fillers dropped, expanded
    assert plan.patterns == ("Redis connection refused: redis: (ECONNREFUSED)", "HTTP 503 on /pay")
    assert plan.alerts == ("RedisDown",)


def test_healthy_question_without_hints_has_nothing_to_search() -> None:
    plan = build_queries(
        "Is anything wrong with payment-service in production?",
        [],
        {"signals": [], "patterns": []},
        service_words=service_words(["payment-service"]),
    )
    assert plan.empty and plan.patterns == ()


def test_question_expansions() -> None:
    assert build_queries("Why are orders timing out?", [], {}).question_query == "orders timeout"
    assert build_queries("API returning HTTP 500", [], {}).question_query == "HTTP 5xx error rate"
    assert build_queries("Orders are failing intermittently", [], {}).question_query == "Orders"


def test_queries_are_bounded_and_deduplicated() -> None:
    plan = build_queries("", [], {"patterns": [f"word{i} word{i}" for i in range(200)]})
    assert len(plan.symptom_query) <= 480
    assert plan.symptom_query.split()[:2] == ["word0", "word1"]


def hit(
    path: str, score: float, matched: int, services: list[str] | None = None, **kw: Any
) -> dict[str, Any]:
    return {
        "path": path,
        "title": path,
        "doc_type": kw.get("doc_type", "runbook"),
        "services": ["payment-service"] if services is None else services,
        "heading_path": kw.get("heading_path", f"{path} > Symptoms"),
        "anchor": "symptoms",
        "snippet": "",
        "score": score,
        "matched_terms": [f"t{i}" for i in range(matched)],
    }


def test_fuse_prefers_service_docs_symptom_matches_and_catalog_runbooks() -> None:
    terms = [f"t{i}" for i in range(10)]
    service = Search("symptoms/service", "q", ["payment-service"], 1.0, "symptoms")
    everywhere = Search("symptoms/all", "q", [], 0.8, "symptoms", generic_bonus=0.5)
    question = Search("question/all", "q", [], 0.25, "question")
    ranked = fuse(
        [
            (service, {"terms": terms, "results": [hit("kb/pool.md", 0.6, 6)]}),
            (
                everywhere,
                {
                    "terms": terms,
                    "results": [
                        hit("kb/generic.md", 0.7, 7, services=[]),
                        hit("kb/pool.md", 0.6, 6),
                        hit("kb/pool.md", 0.2, 1, heading_path="kb/pool.md > Other"),
                    ],
                },
            ),
            (question, {"terms": ["x"], "results": [hit("kb/generic.md", 0.9, 1, services=[])]}),
        ],
        catalog={"pool.md"},
    )
    pool, generic = ranked
    assert pool.path == "kb/pool.md" and pool.in_catalog
    assert round(pool.fused, 3) == round(1.0 * 0.6 + 0.8 * 0.6 + 0.1, 3)
    assert round(generic.fused, 3) == round(1.3 * 0.7 + 0.25 * 0.9, 3)
    # the symptom hit stays the doc's best evidence even though the question hit scored higher
    assert (generic.source, generic.matched, generic.terms) == ("symptoms", 7, 10)
    assert pool.best_heading_path == "kb/pool.md > Symptoms"


def test_relevance_needs_enough_query_words() -> None:
    doc = DocMatch(path="p", title="t", doc_type="runbook", matched=3, terms=10, source="symptoms")
    assert is_relevant(doc, require_symptoms=True)
    assert not is_relevant(
        DocMatch(path="p", title="t", doc_type="runbook", matched=2, terms=10, source="symptoms"),
        require_symptoms=True,
    )
    question_only = DocMatch(
        path="p", title="t", doc_type="runbook", matched=2, terms=2, source="question"
    )
    assert is_relevant(question_only, require_symptoms=False)
    assert not is_relevant(question_only, require_symptoms=True)


def test_extract_sections_by_kind_and_pattern_matching() -> None:
    sections = extract_sections(RUNBOOK)
    assert set(sections) == {"symptoms", "diagnosis", "mitigation", "escalation"}
    assert (
        sections["diagnosis"][0].heading_path
        == "Redis cache outage > Diagnosis > 1. Confirm Redis is down"
    )
    assert sections["diagnosis"][0].anchor == "1-confirm-redis-is-down"
    matches = pattern_matches(
        ["Redis connection refused: redis: (ECONNREFUSED)", "OutOfMemoryError: Java heap space"],
        sections["symptoms"],
    )
    assert [(p, s.heading_path, o) for p, s, o in matches] == [
        ("Redis connection refused: redis: (ECONNREFUSED)", "Redis cache outage > Symptoms", 1.0)
    ]


def test_signals() -> None:
    plan = QueryPlan("q", "", (), ())
    assert derive_signals(KnowledgeAnalysis(plan=plan)) == ["no_relevant_docs"]
    doc = DocMatch(path="p", title="t", doc_type="runbook")
    sections = extract_sections(RUNBOOK)
    found = RunbookFinding(doc=doc, evidence_id="ev-1", link=None, sections=sections)
    assert derive_signals(KnowledgeAnalysis(plan=plan, runbooks=[found])) == [
        "runbook_found",
        "mitigation_available",
    ]
    found.known_issues = pattern_matches(
        ["Redis connection refused ECONNREFUSED"], sections["symptoms"]
    )
    assert "known_issue_documented" in derive_signals(
        KnowledgeAnalysis(plan=plan, runbooks=[found])
    )
