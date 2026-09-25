"""A pragmatic JQL subset: parser + evaluator.

Supported (case-insensitive keywords and field names):
  fields     project, key, status, statusCategory, issuetype (type), priority, resolution,
             labels, component, text, summary, description, created, updated, resolved
  operators  = != IN (..) NOT IN (..)  ~ !~  > >= < <=  IS [NOT] EMPTY|NULL
  logic      AND, OR, NOT, parentheses
  values     "quoted" | 'quoted' | bare words; dates 'YYYY-MM-DD[ HH:MM]' or relative '-7d'
             (units m, h, d, w)
  ordering   ORDER BY <created|updated|resolved|priority|key|status> [ASC|DESC], ...

Anything else (functions such as currentUser(), WAS/CHANGED history operators, custom
fields) is rejected with a JQLError, never silently ignored.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from mock_tickets_mcp.models import Issue


class JQLError(ValueError):
    """Invalid or unsupported JQL. The message is shown to the caller."""


# --------------------------------------------------------------------------- fields

STRING_FIELDS = {
    "project",
    "key",
    "status",
    "statuscategory",
    "issuetype",
    "priority",
    "resolution",
}
LIST_FIELDS = {"labels", "component"}
TEXT_FIELDS = {"text", "summary", "description"}
DATE_FIELDS = {"created", "updated", "resolved"}
FIELD_ALIASES = {
    "issuekey": "key",
    "type": "issuetype",
    "label": "labels",
    "components": "component",
    "createddate": "created",
    "updateddate": "updated",
    "resolutiondate": "resolved",
    "category": "statuscategory",
}
ORDERABLE = {"created", "updated", "resolved", "priority", "key", "status"}
PRIORITY_RANK = {"lowest": 1, "low": 2, "medium": 3, "high": 4, "highest": 5}
SUPPORTED = sorted(STRING_FIELDS | LIST_FIELDS | TEXT_FIELDS | DATE_FIELDS)


def _string_value(issue: Issue, name: str) -> str | None:
    return {
        "project": issue.project,
        "key": issue.key,
        "status": issue.status,
        "statuscategory": issue.status_category,
        "issuetype": issue.issue_type,
        "priority": issue.priority,
        "resolution": issue.resolution,
    }[name]


def _list_value(issue: Issue, name: str) -> list[str]:
    return issue.labels if name == "labels" else issue.components


def _text_value(issue: Issue, name: str) -> str:
    if name == "summary":
        return issue.summary
    if name == "description":
        return issue.description or ""
    return issue.searchable_text()


def _date_value(issue: Issue, name: str) -> datetime | None:
    return {"created": issue.created, "updated": issue.updated, "resolved": issue.resolved}[name]


# --------------------------------------------------------------------------- text search

_WORD = re.compile(r"[a-z0-9]+")


def _stem(word: str) -> str:
    """Tiny stemmer, enough for 'timeouts' ~ 'timeout' and 'queries' ~ 'query'."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def text_matches(haystack: str, query: str) -> bool:
    """Jira-like: every term must occur (any order); a trailing '*' is a prefix match."""
    words = {_stem(w) for w in _WORD.findall(haystack.casefold())}
    terms = query.casefold().split()
    if not terms:
        return False
    for raw in terms:
        prefix = raw.endswith("*")
        parts = _WORD.findall(raw)
        if not parts:
            continue
        for i, part in enumerate(parts):
            if prefix and i == len(parts) - 1:
                if not any(w.startswith(part) for w in words):
                    return False
            elif _stem(part) not in words:
                return False
    return True


# --------------------------------------------------------------------------- dates

_RELATIVE = re.compile(r"^([+-]?)(\d+)([mhdw])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_ABSOLUTE_FORMATS = ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d")


def parse_date(value: str, now: datetime) -> datetime:
    text = value.strip()
    match = _RELATIVE.match(text)
    if match:
        sign, amount, unit = match.groups()
        delta = timedelta(**{_UNITS[unit]: int(amount)})
        return now - delta if sign == "-" else now + delta
    for fmt in _ABSOLUTE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise JQLError(
        f"invalid date '{value}': use 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM' or a relative value like '-7d'"
    )


# --------------------------------------------------------------------------- AST


@dataclass(frozen=True)
class Clause:
    field: str
    op: str  # = != in not_in ~ !~ > >= < <= is is_not
    values: tuple[str, ...]


@dataclass(frozen=True)
class BoolOp:
    op: str  # and | or
    items: tuple[Node, ...]


@dataclass(frozen=True)
class Not:
    item: Node


Node = Clause | BoolOp | Not


@dataclass(frozen=True)
class OrderBy:
    field: str
    descending: bool


@dataclass(frozen=True)
class Query:
    where: Node | None
    order_by: tuple[OrderBy, ...]


# --------------------------------------------------------------------------- tokenizer

_TOKEN = re.compile(
    r"""
    \s*(?:
      (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
    | (?P<op>!=|!~|>=|<=|=|~|>|<)
    | (?P<punct>[(),])
    | (?P<word>[^\s"'(),=!~<>]+)
    )
    """,
    re.VERBOSE,
)
KEYWORDS = {"and", "or", "not", "in", "is", "empty", "null", "order", "by", "asc", "desc"}
UNSUPPORTED_KEYWORDS = {"was", "changed", "during", "before", "after", "on", "from", "to"}


@dataclass(frozen=True)
class Token:
    kind: str  # string | op | punct | word | keyword | end
    value: str


def tokenize(jql: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    text = jql.strip()
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match or match.end() == pos:
            raise JQLError(f"cannot parse JQL near: {text[pos : pos + 20]!r}")
        pos = match.end()
        kind = match.lastgroup
        if kind is None:
            continue
        value = match.group(kind)
        if kind == "string":
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        elif kind == "word" and value.casefold() in KEYWORDS:
            kind, value = "keyword", value.casefold()
        tokens.append(Token(kind, value))
    tokens.append(Token("end", ""))
    return tokens


# --------------------------------------------------------------------------- parser


class _Parser:
    def __init__(self, jql: str) -> None:
        self.tokens = tokenize(jql)
        self.pos = 0

    def peek(self, offset: int = 0) -> Token:
        return self.tokens[min(self.pos + offset, len(self.tokens) - 1)]

    def next(self) -> Token:
        token = self.peek()
        self.pos += 1
        return token

    def accept(self, kind: str, value: str | None = None) -> Token | None:
        token = self.peek()
        if token.kind == kind and (value is None or token.value == value):
            self.pos += 1
            return token
        return None

    def expect(self, kind: str, value: str | None = None) -> Token:
        token = self.accept(kind, value)
        if token is None:
            got = self.peek().value or "end of query"
            raise JQLError(f"expected {value or kind} but found '{got}'")
        return token

    def parse(self) -> Query:
        where: Node | None = None
        at_order = self.peek().kind == "keyword" and self.peek().value == "order"
        if not at_order and self.peek().kind != "end":
            where = self.parse_or()
        order: list[OrderBy] = []
        if self.accept("keyword", "order"):
            self.expect("keyword", "by")
            while True:
                token = self.next()
                if token.kind not in ("word", "string"):
                    raise JQLError("ORDER BY needs a field name")
                name = _field_name(token.value)
                if name not in ORDERABLE:
                    raise JQLError(
                        f"cannot ORDER BY '{token.value}' (supported: {', '.join(sorted(ORDERABLE))})"
                    )
                descending = False
                if self.accept("keyword", "desc"):
                    descending = True
                else:
                    self.accept("keyword", "asc")
                order.append(OrderBy(name, descending))
                if not self.accept("punct", ","):
                    break
        if self.peek().kind != "end":
            raise JQLError(f"unexpected '{self.peek().value}'")
        return Query(where, tuple(order))

    def parse_or(self) -> Node:
        items = [self.parse_and()]
        while self.accept("keyword", "or"):
            items.append(self.parse_and())
        return items[0] if len(items) == 1 else BoolOp("or", tuple(items))

    def parse_and(self) -> Node:
        items = [self.parse_not()]
        while self.accept("keyword", "and"):
            items.append(self.parse_not())
        return items[0] if len(items) == 1 else BoolOp("and", tuple(items))

    def parse_not(self) -> Node:
        if self.accept("keyword", "not"):
            return Not(self.parse_not())
        if self.accept("punct", "("):
            node = self.parse_or()
            self.expect("punct", ")")
            return node
        return self.parse_clause()

    def parse_clause(self) -> Clause:
        token = self.next()
        if token.kind not in ("word", "string"):
            raise JQLError(f"expected a field name but found '{token.value or 'end of query'}'")
        if self.peek().kind == "punct" and self.peek().value == "(":
            raise JQLError(f"JQL functions are not supported: {token.value}()")
        name = _field_name(token.value)
        if name not in SUPPORTED:
            raise JQLError(
                f"unsupported JQL field '{token.value}' (supported: {', '.join(SUPPORTED)})"
            )
        op = self._operator()
        if op in ("in", "not_in"):
            values = self._list()
        elif op in ("is", "is_not"):
            empty = self.next()
            if not (empty.kind == "keyword" and empty.value in ("empty", "null")):
                raise JQLError("IS / IS NOT must be followed by EMPTY or NULL")
            values = ()
        else:
            values = (self._value(),)
        clause = Clause(name, op, values)
        _validate(clause)
        return clause

    def _operator(self) -> str:
        token = self.next()
        if token.kind == "op":
            return token.value
        if token.kind == "keyword" and token.value == "in":
            return "in"
        if token.kind == "keyword" and token.value == "not":
            self.expect("keyword", "in")
            return "not_in"
        if token.kind == "keyword" and token.value == "is":
            return "is_not" if self.accept("keyword", "not") else "is"
        if token.kind == "word" and token.value.casefold() in UNSUPPORTED_KEYWORDS:
            raise JQLError(f"history operators are not supported: {token.value.upper()}")
        raise JQLError(f"expected an operator but found '{token.value or 'end of query'}'")

    def _value(self) -> str:
        token = self.next()
        if token.kind not in ("word", "string"):
            raise JQLError(f"expected a value but found '{token.value or 'end of query'}'")
        if token.kind == "word" and self.peek().kind == "punct" and self.peek().value == "(":
            raise JQLError(f"JQL functions are not supported: {token.value}()")
        return token.value

    def _list(self) -> tuple[str, ...]:
        self.expect("punct", "(")
        values = [self._value()]
        while self.accept("punct", ","):
            values.append(self._value())
        self.expect("punct", ")")
        return tuple(values)


def _field_name(raw: str) -> str:
    name = raw.casefold()
    return FIELD_ALIASES.get(name, name)


def _validate(clause: Clause) -> None:
    allowed: set[str]
    if clause.field in STRING_FIELDS or clause.field in LIST_FIELDS:
        allowed = {"=", "!=", "in", "not_in", "is", "is_not"}
    elif clause.field in TEXT_FIELDS:
        allowed = {"~", "!~"}
    else:
        allowed = {"=", "!=", ">", ">=", "<", "<=", "is", "is_not"}
    if clause.op not in allowed:
        pretty = clause.op.replace("_", " ").upper()
        raise JQLError(f"operator '{pretty}' is not supported for field '{clause.field}'")


def parse(jql: str) -> Query:
    return _Parser(jql).parse()


# --------------------------------------------------------------------------- evaluation


def _eq(a: str | None, b: str) -> bool:
    return a is not None and a.casefold() == b.casefold()


def matches(node: Node | None, issue: Issue, now: datetime) -> bool:
    if node is None:
        return True
    if isinstance(node, Not):
        return not matches(node.item, issue, now)
    if isinstance(node, BoolOp):
        results = (matches(item, issue, now) for item in node.items)
        return all(results) if node.op == "and" else any(results)
    return _clause(node, issue, now)


def _clause(clause: Clause, issue: Issue, now: datetime) -> bool:
    name, op, values = clause.field, clause.op, clause.values
    if name in STRING_FIELDS:
        current = _string_value(issue, name)
        if name == "resolution" and values and values[0].casefold() == "unresolved":
            current = current or "Unresolved"
        if op in ("is", "is_not"):
            return (current is None) == (op == "is")
        hit = any(_eq(current, v) for v in values)
        return hit if op in ("=", "in") else (current is not None and not hit)
    if name in LIST_FIELDS:
        items = _list_value(issue, name)
        if op in ("is", "is_not"):
            return (not items) == (op == "is")
        hit = any(_eq(item, v) for item in items for v in values)
        return hit if op in ("=", "in") else not hit
    if name in TEXT_FIELDS:
        hit = text_matches(_text_value(issue, name), values[0])
        return hit if op == "~" else not hit
    current_date = _date_value(issue, name)
    if op in ("is", "is_not"):
        return (current_date is None) == (op == "is")
    if current_date is None:
        return False
    target = parse_date(values[0], now)
    compare: dict[str, Callable[[datetime, datetime], bool]] = {
        "=": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
        ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b,
        "<": lambda a, b: a < b,
        "<=": lambda a, b: a <= b,
    }
    return compare[op](current_date, target)


def validate_dates(node: Node | None, now: datetime) -> None:
    """Fail fast on invalid date literals (even when no issue reaches the clause)."""
    if node is None:
        return
    if isinstance(node, Not):
        validate_dates(node.item, now)
    elif isinstance(node, BoolOp):
        for item in node.items:
            validate_dates(item, now)
    elif node.field in DATE_FIELDS and node.values:
        parse_date(node.values[0], now)


def _sort_key(issue: Issue, name: str) -> Any:
    if name == "priority":
        return PRIORITY_RANK.get((issue.priority or "").casefold(), 0)
    if name == "key":
        return (issue.project, issue.number)
    if name == "status":
        return issue.status.casefold()
    return _date_value(issue, name)


def search(issues: list[Issue], query: Query, now: datetime) -> list[Issue]:
    validate_dates(query.where, now)
    hits = [i for i in issues if matches(query.where, i, now)]
    order = query.order_by or (OrderBy("created", True),)
    # Stable multi-key sort: apply the least significant key first; None always last.
    for key in reversed(order):
        present = [i for i in hits if _sort_key(i, key.field) is not None]
        missing = [i for i in hits if _sort_key(i, key.field) is None]
        present.sort(key=lambda i, f=key.field: _sort_key(i, f), reverse=key.descending)  # type: ignore[misc]
        hits = present + missing
    return hits
