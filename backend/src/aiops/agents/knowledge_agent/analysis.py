"""Deterministic knowledge analysis: symptoms -> queries -> ranked runbooks -> cited sections.

No model is involved: queries are built from structured hints (signals, log
patterns, alert names) and the question; ranking fuses the search server's
scores; relevance, "known issue" and "mitigation available" are rules over the
retrieved text. The LLM only summarizes what this module found.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aiops.knowledge.markdown import Section, parse_sections

#: Signal vocabulary (must match config/prompts/knowledge/v*.md). All data-derived.
SIGNALS = ("runbook_found", "known_issue_documented", "mitigation_available", "no_relevant_docs")

#: Upstream signals (Log/Metrics/Alert agents) -> words runbooks use for that symptom.
SIGNAL_TERMS: dict[str, str] = {
    "db_timeout_errors_up": "database connection pool timeout",
    "error_rate_up": "error rate 5xx",
    "oom_errors": "OutOfMemoryError OOMKilled memory leak heap",
    "service_restarts": "restarts OOMKilled crashloop",
    "dependency_timeouts": "downstream dependency timeout",
    "slow_queries": "slow query database",
    "capacity_degraded": "ready replicas capacity rollout",
    "upstream_unavailable": "upstream unavailable 503",
    "cache_connection_errors": "redis cache connection refused",
    "deployment_detected": "deployment rollback release",
    "latency_up": "high latency p95",
    "pod_crashloop": "CrashLoopBackOff restarts",
    "image_pull_errors": "ImagePullBackOff image tag",
}

#: Question words that say nothing about the symptom (fillers, environments, verbs).
GENERIC_WORDS = frozenset(
    [
        "a",
        "about",
        "all",
        "am",
        "an",
        "and",
        "any",
        "anything",
        "api",
        "app",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "for",
        "from",
        "get",
        "getting",
        "going",
        "had",
        "has",
        "have",
        "having",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "me",
        "my",
        "no",
        "not",
        "now",
        "of",
        "on",
        "or",
        "our",
        "out",
        "over",
        "please",
        "prod",
        "production",
        "service",
        "services",
        "so",
        "some",
        "something",
        "staging",
        "still",
        "system",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "this",
        "to",
        "today",
        "up",
        "us",
        "was",
        "we",
        "were",
        "what",
        "whats",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "wrong",
        "you",
        "your",
        "right",
        "currently",
        "issue",
        "issues",
        "problem",
        "problems",
        "happening",
        "going",
        "on",
        "see",
        "seeing",
        "returning",
        "return",
        "returns",
        "requests",
        "request",
        "users",
        "customers",
        "failing",
        "fail",
        "failed",
        "fails",
        "failure",
        "failures",
        "broken",
        "intermittently",
        "intermittent",
        "sometimes",
        "randomly",
    ]
)

#: Question phrases -> the words runbooks use.
QUESTION_EXPANSIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\btim(ing|ed|es) out\b", re.I), "timeout"),
    (re.compile(r"\bslow(ness|er)?\b", re.I), "slow latency"),
    (re.compile(r"\b5\d\d\b|\b5xx\b", re.I), "5xx error rate"),
    (re.compile(r"\bcrash(es|ing|ed)?\b", re.I), "crashloop restarts"),
    (re.compile(r"\bout of memory\b|\boom\b", re.I), "OutOfMemoryError OOMKilled"),
)

_PLACEHOLDER = re.compile(r"<[A-Z]+>")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-/]*[A-Za-z0-9]|[A-Za-z0-9]")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN = re.compile(r"[a-z0-9]+")

MAX_QUERY_CHARS = 480
STEM = 6  # crude prefix stemming for local text matching ("connections" ~ "connection")
MIN_PATTERN_OVERLAP = 0.6  # share of a pattern's words a runbook section must contain

#: Section kinds, by the H2 heading they live under.
SECTION_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("known_issues", ("known issue", "known problem", "past incident")),
    ("symptoms", ("symptom", "signs", "how to recognize")),
    ("diagnosis", ("diagnos", "investigat", "triage", "troubleshoot")),
    ("mitigation", ("mitigat", "remediat", "workaround", "fix")),
    ("rollback", ("rollback", "roll back", "revert")),
    ("escalation", ("escalat", "ownership", "on-call", "contact")),
    ("summary", ("summary", "overview", "context")),
)
CITED_KINDS = ("diagnosis", "mitigation", "rollback", "escalation")
MATCH_KINDS = ("symptoms", "known_issues")


# --------------------------------------------------------------------------- queries


def _dedupe_words(parts: Iterable[str], max_chars: int) -> str:
    seen: set[str] = set()
    words: list[str] = []
    length = 0
    for part in parts:
        for word in _WORD.findall(part):
            key = word.casefold()
            if key in seen:
                continue
            if length + len(word) + 1 > max_chars:
                return " ".join(words)
            seen.add(key)
            words.append(word)
            length += len(word) + 1
    return " ".join(words)


def pattern_text(template: str) -> str:
    """'Redis connection refused: redis:<NUM> (ECONNREFUSED)' -> 'Redis connection refused: redis: (ECONNREFUSED)'."""
    return re.sub(r"\s+", " ", _PLACEHOLDER.sub(" ", template)).strip()


def _as_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, str | int | float) and str(v).strip()]
    return []


@dataclass(frozen=True)
class QueryPlan:
    symptom_query: str  # from hints + context symptoms (the strong source)
    question_query: str  # symptom-bearing words of the question (the weak source)
    patterns: tuple[str, ...]  # log patterns / symptom phrases, for known-issue matching
    alerts: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not (self.symptom_query or self.question_query)


def build_queries(
    question: str,
    symptoms: Sequence[str],
    hints: Mapping[str, Any],
    *,
    service_words: Iterable[str] = (),
    signal_terms: Mapping[str, str] = SIGNAL_TERMS,
) -> QueryPlan:
    """Hints: ``{"signals": [...], "patterns": [...], "alerts": [...], "keywords": [...]}``."""
    patterns = [pattern_text(p) for p in _as_strings(hints.get("patterns")) if pattern_text(p)]
    alerts = _as_strings(hints.get("alerts"))
    alert_words = [f"{a} {_CAMEL.sub(' ', a)}" for a in alerts]
    signal_words = [signal_terms.get(s, "") for s in _as_strings(hints.get("signals"))]
    keywords = _as_strings(hints.get("keywords"))
    symptom_query = _dedupe_words(
        [*patterns, *symptoms, *alert_words, *keywords, *signal_words], MAX_QUERY_CHARS
    )

    expanded = question
    for regex, replacement in QUESTION_EXPANSIONS:
        expanded = regex.sub(f" {replacement} ", expanded)
    generic = GENERIC_WORDS | {w.casefold() for w in service_words}
    words = [
        w
        for w in _WORD.findall(expanded)
        if w.casefold() not in generic and w.casefold().rstrip("s") not in generic
    ]
    question_query = _dedupe_words(words, MAX_QUERY_CHARS)
    return QueryPlan(
        symptom_query=symptom_query,
        question_query=question_query,
        patterns=tuple([*patterns, *symptoms]),
        alerts=tuple(alerts),
    )


def service_words(names: Iterable[str]) -> set[str]:
    """Catalog names/aliases -> words to drop from the question ('payment-service' -> payment)."""
    words: set[str] = set()
    for name in names:
        words.add(name.casefold())
        words.update(_TOKEN.findall(name.casefold()))
    return words


# --------------------------------------------------------------------------- ranking


@dataclass
class Search:
    label: str  # "symptoms/service", "symptoms/all", "question/service", ...
    query: str
    services: list[str]
    weight: float
    source: str  # "symptoms" | "question"
    generic_bonus: float = 0.0  # extra weight for docs without services (they apply to all)


@dataclass
class DocMatch:
    path: str
    title: str
    doc_type: str
    fused: float = 0.0
    best_score: float = 0.0
    best_heading_path: str = ""
    best_anchor: str = ""
    best_snippet: str = ""
    matched: int = 0
    terms: int = 0
    source: str = ""  # query source of the best hit
    in_catalog: bool = False

    @property
    def coverage(self) -> float:
        return self.matched / self.terms if self.terms else 0.0


CATALOG_BOOST = 0.1
MIN_COVERAGE = 0.3
MIN_MATCHED_TERMS = 3


def fuse(results: Sequence[tuple[Search, Mapping[str, Any]]], catalog: set[str]) -> list[DocMatch]:
    """Weighted fusion of search results -> documents, best first.

    A document scores ``sum(weight * best section score)`` over the searches that
    found it (so a service-tagged runbook found by both the service-filtered and
    the global search outranks a generic one), plus a small boost when the service
    catalog lists it. ``catalog`` holds file names (``redis-outage.md``) or paths.
    """
    docs: dict[str, DocMatch] = {}
    for search, data in results:
        terms = len(data.get("terms") or [])
        best_in_search: dict[str, Mapping[str, Any]] = {}
        for hit in data.get("results") or []:
            path = str(hit.get("path", ""))
            if path and path not in best_in_search:
                best_in_search[path] = hit
        for path, hit in best_in_search.items():
            doc = docs.setdefault(
                path,
                DocMatch(
                    path=path,
                    title=str(hit.get("title", path)),
                    doc_type=str(hit.get("doc_type", "doc")),
                    in_catalog=path in catalog or path.rsplit("/", 1)[-1] in catalog,
                ),
            )
            score = float(hit.get("score") or 0.0)
            generic = not hit.get("services")
            doc.fused += (search.weight + (search.generic_bonus if generic else 0.0)) * score
            matched = len(hit.get("matched_terms") or [])
            # Keep the strongest evidence: symptom queries beat question queries.
            stronger = (search.source == "symptoms" and doc.source != "symptoms") or (
                search.source == doc.source and score > doc.best_score
            )
            if not doc.source or stronger:
                doc.best_score = score
                doc.best_heading_path = str(hit.get("heading_path", ""))
                doc.best_anchor = str(hit.get("anchor", ""))
                doc.best_snippet = str(hit.get("snippet", ""))
                doc.matched, doc.terms, doc.source = matched, terms, search.source
    for doc in docs.values():
        if doc.in_catalog:
            doc.fused += CATALOG_BOOST
    return sorted(docs.values(), key=lambda d: (-d.fused, d.path))


def is_relevant(doc: DocMatch, *, require_symptoms: bool) -> bool:
    """Enough of the query's words in one section (not a single common word).

    With symptoms available, only symptom-query matches count: the question alone
    ("payments are slow") is too thin to call a runbook relevant.
    """
    if require_symptoms and doc.source != "symptoms":
        return False
    needed = min(MIN_MATCHED_TERMS, doc.terms)
    return doc.terms > 0 and doc.matched >= needed and doc.coverage >= MIN_COVERAGE


# --------------------------------------------------------------------------- sections


def section_kind(section: Section) -> str | None:
    """Kind of a section from its H2 ancestor (subsections inherit it)."""
    heading = (section.path[1] if len(section.path) > 1 else section.heading).casefold()
    for kind, keys in SECTION_KINDS:
        if any(key in heading for key in keys):
            return kind
    return None


@dataclass
class ExtractedSection:
    kind: str
    heading_path: str
    anchor: str
    text: str


def extract_sections(markdown: str) -> dict[str, list[ExtractedSection]]:
    """Group a runbook's sections by kind (symptoms, known_issues, diagnosis, ...)."""
    grouped: dict[str, list[ExtractedSection]] = {}
    for section in parse_sections(markdown):
        kind = section_kind(section)
        if kind is None or not section.text:
            continue
        grouped.setdefault(kind, []).append(
            ExtractedSection(kind, section.heading_path, section.anchor, section.text)
        )
    return grouped


def extract_sections_by_heading(markdown: str) -> list[ExtractedSection]:
    """Every non-empty section (service docs use their own headings, not runbook kinds)."""
    return [
        ExtractedSection("section", s.heading_path, s.anchor, s.text)
        for s in parse_sections(markdown)
        if s.text
    ]


def section_text(sections: Sequence[ExtractedSection], limit: int) -> str:
    """Sub-sections joined under their own heading, truncated at ``limit`` chars."""
    parts = []
    for s in sections:
        heading = s.heading_path.rsplit(" > ", 1)[-1]
        parts.append(f"{heading}:\n{s.text}" if len(sections) > 1 else s.text)
    text = "\n\n".join(parts)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _stems(text: str) -> set[str]:
    return {t[:STEM] for t in _TOKEN.findall(text.casefold()) if len(t) >= 3 and not t.isdigit()}


def pattern_matches(
    patterns: Sequence[str], sections: Sequence[ExtractedSection]
) -> list[tuple[str, ExtractedSection, float]]:
    """(pattern, section, overlap) where a symptom/known-issue section quotes the pattern."""
    matches = []
    for pattern in patterns:
        words = _stems(pattern) - {w[:STEM] for w in GENERIC_WORDS}
        if len(words) < 2:
            continue
        best: tuple[ExtractedSection, float] | None = None
        for section in sections:
            overlap = len(words & _stems(section.text)) / len(words)
            if overlap >= MIN_PATTERN_OVERLAP and (best is None or overlap > best[1]):
                best = (section, overlap)
        if best:
            matches.append((pattern, best[0], round(best[1], 2)))
    return matches


# --------------------------------------------------------------------------- result


@dataclass
class RunbookFinding:
    doc: DocMatch
    evidence_id: str
    link: str | None
    sections: dict[str, list[ExtractedSection]] = field(default_factory=dict)
    known_issues: list[tuple[str, ExtractedSection, float]] = field(default_factory=list)

    @property
    def has_mitigation(self) -> bool:
        return bool(self.sections.get("mitigation") or self.sections.get("rollback"))


@dataclass
class KnowledgeAnalysis:
    plan: QueryPlan
    ranked: list[DocMatch] = field(default_factory=list)  # every document found, fused order
    runbooks: list[RunbookFinding] = field(default_factory=list)  # relevant, fetched, top N
    context_docs: list[tuple[str, str, str | None]] = field(
        default_factory=list
    )  # (path, ev, link)
    signals: list[str] = field(default_factory=list)


def derive_signals(analysis: KnowledgeAnalysis) -> list[str]:
    signals: set[str] = set()
    if analysis.runbooks:
        signals.add("runbook_found")
        if any(r.known_issues for r in analysis.runbooks):
            signals.add("known_issue_documented")
        if any(r.has_mitigation for r in analysis.runbooks):
            signals.add("mitigation_available")
    else:
        signals.add("no_relevant_docs")
    return [s for s in SIGNALS if s in signals]
