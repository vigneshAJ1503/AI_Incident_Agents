"""Deterministic ticket analysis: symptom terms, JQL, relevance and signals.

The LLM reads the result; it doesn't decide which ticket is a known issue.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aiops.mcp.tickets import Ticket

#: Signal vocabulary (must match config/prompts/tickets/v*.md). All are data-derived.
SIGNALS = (
    "known_issue_open",
    "dependency_known_issue_open",
    "similar_past_incident",
    "related_open_tickets",
    "no_related_tickets",
)
STRONG_SIGNALS = frozenset(
    {"known_issue_open", "dependency_known_issue_open", "similar_past_incident"}
)


@dataclass(frozen=True)
class SymptomTerm:
    """A symptom: how to recognise it in questions/signals, and what to search tickets for."""

    name: str
    triggers: re.Pattern[str]
    search: tuple[str, ...]  # JQL text terms; 'word*' = prefix


def _term(name: str, triggers: str, *search: str) -> SymptomTerm:
    return SymptomTerm(name, re.compile(triggers, re.IGNORECASE), search)


#: Default vocabulary. Deliberately specific: generic words ("failing", "broken", "error",
#: "wrong") never become search terms, so a vague question can't "match" a known issue.
DEFAULT_TERMS: tuple[SymptomTerm, ...] = (
    _term("timeout", r"time[ds]?[ _-]?outs?\b|timing out|deadline exceeded", "timeout"),
    _term("http_500", r"\b500\b|internal server error|\b5xx\b", "500"),
    _term("http_503", r"\b503\b|service unavailable", "503"),
    _term("oom", r"\boom|out of memory|outofmemory|memory leak", "oom*", "memory"),
    _term("latency", r"\bslow|latenc|\bp9[059]\b|response time", "slow", "latency"),
    _term("database", r"\bdb\b|database|connection pool|\bpool\b", "database", "pool"),
    _term("cache", r"\bcache|redis", "cache", "redis"),
    _term("crash", r"crash|restart", "crash*", "restart*"),
    _term("image_pull", r"imagepull|image pull|bad image|pull image", "imagepullbackoff", "image"),
)


def load_terms(overrides: Mapping[str, Any] | None) -> tuple[SymptomTerm, ...]:
    """``capabilities.tickets.settings.symptom_terms`` ({name: {triggers, search}}) replaces
    the defaults (portability: another company's vocabulary)."""
    if not overrides:
        return DEFAULT_TERMS
    return tuple(
        _term(name, str(spec["triggers"]), *[str(s) for s in spec["search"]])
        for name, spec in overrides.items()
    )


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _strings(item)


def symptom_terms(
    question: str, symptoms: Sequence[str], hints: Mapping[str, Any], terms: Sequence[SymptomTerm]
) -> list[SymptomTerm]:
    """Symptom terms mentioned by the question, earlier findings' symptoms or hints."""
    texts = [question, *symptoms, *_strings(hints)]
    corpus = " ".join(t.replace("_", " ") for t in texts)
    return [t for t in terms if t.triggers.search(corpus)]


# --------------------------------------------------------------------------- text matching

_WORD = re.compile(r"[a-z0-9]+")


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def mentions(text: str, term: str) -> bool:
    """Word match with light stemming; 'oom*' is a prefix match (like JQL text search)."""
    words = {_stem(w) for w in _WORD.findall(text.casefold())}
    needle = term.casefold()
    if needle.endswith("*"):
        prefix = needle.rstrip("*")
        return any(w.startswith(prefix) for w in words)
    parts = _WORD.findall(needle)
    return bool(parts) and all(_stem(p) in words for p in parts)


# --------------------------------------------------------------------------- JQL


def jql_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def jql_list(values: Iterable[str]) -> str:
    return "(" + ", ".join(jql_value(v) for v in values) + ")"


def recency_clause(cutoff: datetime) -> str:
    """Open issues, or issues resolved since ``cutoff`` (absolute date: replayable)."""
    return f'(statusCategory != Done OR resolved >= "{cutoff:%Y-%m-%d}")'


def scope_jql(
    project: str, components: Sequence[str], labels: Sequence[str], cutoff: datetime
) -> str:
    parts = []
    if components:
        parts.append(f"component in {jql_list(components)}")
    if labels:
        parts.append(f"labels in {jql_list(labels)}")
    return (
        f"project = {jql_value(project)} AND ({' OR '.join(parts)}) AND {recency_clause(cutoff)} "
        "ORDER BY updated DESC"
    )


def keyword_jql(project: str, terms: Sequence[SymptomTerm], cutoff: datetime) -> str:
    words = dict.fromkeys(s for t in terms for s in t.search)
    text = " OR ".join(f"text ~ {jql_value(w)}" for w in words)
    return f"project = {jql_value(project)} AND ({text}) AND {recency_clause(cutoff)} ORDER BY updated DESC"


# --------------------------------------------------------------------------- relevance


@dataclass(frozen=True)
class ServiceScope:
    name: str
    components: tuple[str, ...]
    labels: tuple[str, ...]

    def owns(self, ticket: Ticket) -> bool:
        comps = {c.casefold() for c in ticket.components}
        labels = {label.casefold() for label in ticket.labels}
        return bool(
            comps & {c.casefold() for c in self.components}
            or labels & {label.casefold() for label in self.labels}
        )


@dataclass
class Assessment:
    ticket: Ticket
    relation: str  # service | dependency | other
    related_service: str | None
    matched: list[str]  # symptom term names
    evidence_id: str | None = None

    @property
    def category(self) -> str:
        t = self.ticket
        if self.relation == "service" and self.matched:
            return "known_issue" if t.is_open else "similar_past"
        if self.relation == "dependency" and self.matched and t.is_open:
            return "dependency_known_issue"
        if self.relation == "service":
            return "open_on_service" if t.is_open else "resolved_on_service"
        if self.relation == "dependency":
            return "dependency_other"
        return "keyword_other" if self.matched else "unrelated"

    @property
    def rank(self) -> int:
        order = [
            "known_issue",
            "dependency_known_issue",
            "similar_past",
            "open_on_service",
            "keyword_other",
            "resolved_on_service",
            "dependency_other",
            "unrelated",
        ]
        return order.index(self.category)


def assess(
    ticket: Ticket,
    service: ServiceScope | None,
    dependencies: Sequence[ServiceScope],
    terms: Sequence[SymptomTerm],
) -> Assessment:
    relation, related = "other", None
    dep = next((d for d in dependencies if d.owns(ticket)), None)
    if service and service.owns(ticket):
        relation, related = "service", service.name
    elif dep:
        relation, related = "dependency", dep.name
    haystack = " ".join([ticket.text(), *ticket.labels])
    matched = [t.name for t in terms if any(mentions(haystack, s) for s in t.search)]
    return Assessment(ticket, relation, related, matched)


@dataclass
class TicketAnalysis:
    project: str
    service: ServiceScope | None
    dependencies: list[ServiceScope]
    terms: list[SymptomTerm]
    cutoff: datetime
    assessments: list[Assessment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def of(self, category: str) -> list[Assessment]:
        return [a for a in self.assessments if a.category == category]

    @property
    def relevant(self) -> list[Assessment]:
        return sorted(
            (a for a in self.assessments if a.category != "unrelated"),
            key=lambda a: (a.rank, a.ticket.key),
        )

    @property
    def signals(self) -> list[str]:
        found: set[str] = set()
        if self.of("known_issue"):
            found.add("known_issue_open")
        if self.of("dependency_known_issue"):
            found.add("dependency_known_issue_open")
        if self.of("similar_past"):
            found.add("similar_past_incident")
        if self.of("open_on_service"):
            found.add("related_open_tickets")
        if not self.relevant:
            found.add("no_related_tickets")
        return [s for s in SIGNALS if s in found]

    @property
    def strong(self) -> bool:
        return bool(STRONG_SIGNALS & set(self.signals))

    # -- text for the LLM ------------------------------------------------------------------

    def _line(self, a: Assessment) -> str:
        t = a.ticket
        created = f"{t.created:%Y-%m-%d}" if t.created else "?"
        resolved = f" | resolved {t.resolved:%Y-%m-%d}" if t.resolved else ""
        matches = f" | matches: {', '.join(a.matched)}" if a.matched else ""
        service = f" | {a.related_service}" if a.related_service else ""
        ref = f"[{a.evidence_id}] " if a.evidence_id else ""
        return (
            f"  - {ref}{t.key} | {t.status} | {t.issue_type} | {t.priority or '-'}{service} | "
            f"created {created}{resolved}{matches} | {t.summary}"
        )

    def lines(self) -> list[str]:
        scope = f"project {self.project}"
        if self.service:
            scope += (
                f"; service {self.service.name} (components {', '.join(self.service.components) or '-'}; "
                f"labels {', '.join(self.service.labels) or '-'})"
            )
        if self.dependencies:
            scope += "; dependencies " + ", ".join(d.name for d in self.dependencies)
        terms = ", ".join(f"{t.name} ({'/'.join(t.search)})" for t in self.terms) or "none"
        out = [
            f"Search scope: {scope}. Open tickets, or resolved since {self.cutoff:%Y-%m-%d}.",
            f"Symptom terms (from the question, symptoms and hints): {terms}.",
        ]
        sections = [
            ("known_issue", "KNOWN ISSUES (open, same service, symptom match):"),
            ("dependency_known_issue", "Open issues on DEPENDENCIES matching the symptoms:"),
            (
                "similar_past",
                "SIMILAR PAST INCIDENTS (resolved recently, same service, symptom match):",
            ),
            (
                "open_on_service",
                "Other open tickets on the service (no symptom match; context only):",
            ),
            ("resolved_on_service", "Recently resolved tickets on the service (no symptom match):"),
            ("keyword_other", "Symptom matches on unrelated services (weak):"),
            ("dependency_other", "Other tickets on dependencies (no symptom match):"),
        ]
        for category, title in sections:
            items = self.of(category)
            if items:
                out.append(title)
                out.extend(self._line(a) for a in sorted(items, key=lambda a: a.ticket.key))
        if not self.relevant:
            out.append("No related tickets found.")
        out.extend(self.notes)
        out.append(f"Deterministic signals: {', '.join(self.signals) or 'none'}")
        return out
