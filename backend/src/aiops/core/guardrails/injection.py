"""Prompt-injection heuristics for tool output (PR-042, MASTER_PLAN §16.4).

Tool output (log lines, ticket bodies, runbooks, commit messages, alert annotations) is
attacker-reachable: anyone who can write a log line can write "ignore previous
instructions". The primary defenses are structural and don't depend on this module:

  * tool output is wrapped in ``<tool_output>`` and the prompts say it is data;
  * agents only ever get read tools (``tool_allowlist``); write tools need a human approval;
  * findings may only cite evidence ids the agent really produced;
  * deterministic data signals are authoritative in every agent's ``finalize``.

This module adds *detection* on top: it flags text that looks like instructions to the
model so the report can say "this evidence tried to steer the investigation" and the audit
log records the attempt. It also neutralises fake ``<tool_output>`` delimiters, including
case, whitespace, full-width and zero-width variants, so the data can't close its wrapper.

Matching runs on a normalised copy (NFKC, zero-width characters removed, common Cyrillic/
Greek homoglyphs folded to Latin, lower case); the data the agent keeps is not altered
beyond the delimiter escaping.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

#: Only this much of one tool result is scanned (the LLM sees far less: max_tool_output_chars).
MAX_SCAN_CHARS = 200_000

#: Characters that render as nothing and are used to split keywords ("ign\u200bore").
_INVISIBLE = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\u2061\u2062\u2063\u2064\ufeff\u00ad\u180e"),
    None,
)
#: Lower-case Cyrillic/Greek letters (text is case-folded first) and their Latin look-alikes.
_CONFUSABLE = "\u0430\u0435\u043e\u0440\u0441\u0445\u0443\u0456\u0458\u0455\u04bb\u0501\u0261\u0578\u03bd\u03bf\u03b1\u03b5\u03b9\u03ba\u03c4\u03c5\u03c1\u03b2\u03b7\u03bc\u03c7\u03b6\u0432\u043a\u043c\u043d\u0442"
_HOMOGLYPHS = str.maketrans(_CONFUSABLE, "aeopcxyijshdgnvoaeiktupbhmxzbkmht")

#: kind -> patterns matched against the normalised text.
_RULES: dict[str, list[re.Pattern[str]]] = {
    "ignore_instructions": [
        re.compile(
            r"\b(?:ignore|disregard|forget|override|bypass|skip)\b.{0,40}?"
            r"\b(?:previous|prior|above|earlier|all|any|your|the|system|preceding)\b.{0,30}?"
            r"\b(?:instructions?|rules?|prompts?|directives?|guidelines?|guardrails?|context)\b"
        ),
        re.compile(r"\bnew\s+(?:system\s+)?instructions?\s*:"),
        re.compile(r"\byou\s+are\s+now\b|\bact\s+as\s+(?:an?\s+)?(?:admin|root|system)\b"),
        re.compile(r"\b(?:jailbreak|developer\s+mode|dan\s+mode)\b"),
    ],
    "role_markers": [
        re.compile(r"<\|?\s*(?:im_start|im_end|system|endoftext)\s*\|?>"),
        re.compile(r"\[/?inst\]|<<\s*/?sys\s*>>"),
        re.compile(r"^\s*(?:system|assistant)\s*:", re.MULTILINE),
    ],
    "fake_delimiter": [re.compile(r"<\s*/?\s*tool_output\b")],
    "fake_evidence_id": [re.compile(r"\bevidence_id\s*[:=]\s*ev-[0-9a-z]+")],
    "tool_call_request": [
        re.compile(
            r"\b(?:call|invoke|execute|trigger)\b.{0,30}?\b(?:the\s+)?"
            r"(?:tool|function)\b"
        ),
        re.compile(r"\b(?:call|invoke)\s+(?:the\s+)?`?[a-z]+_[a-z_]+`?"),
        re.compile(r"\b(?:jira_)?(?:create_issue|add_comment|update_issue|transition_issue)\b"),
        re.compile(r"\"(?:name|tool|function)\"\s*:\s*\"[a-z_]+\"\s*,\s*\"(?:arguments|args)\""),
        re.compile(r"\b(?:submit|create_issue|add_comment|update_issue)\s*\(\s*[{\"']"),
    ],
    "write_action_request": [
        re.compile(
            r"\b(?:create|open|file|close|delete|update|comment\s+on)\b.{0,20}?"
            r"\b(?:a\s+|the\s+|an\s+)?(?:ticket|issue|jira|incident)\b.{0,40}?"
            r"\b(?:now|immediately|automatically|without\s+approval)\b"
        ),
        re.compile(
            r"\b(?:restart|rollback|roll\s+back|scale|delete|kubectl\s+(?:delete|exec|apply|patch))"
            r"\b.{0,40}?\b(?:now|immediately|without\s+(?:approval|asking))\b"
        ),
        re.compile(r"\bwithout\s+(?:human\s+)?approval\b|\bauto[-\s]?approve"),
    ],
    "exfiltration": [
        re.compile(
            r"\b(?:print|reveal|show|send|leak|dump|exfiltrate|expose|disclose)\b.{0,40}?"
            r"\b(?:env(?:ironment)?\s*(?:vars?|variables?)|api[_\s-]?keys?|secrets?|"
            r"credentials?|passwords?|tokens?|system\s+prompt)\b"
        ),
        re.compile(r"\b(?:curl|wget|send|upload|exfiltrate)\b.{0,40}?https?://"),
        re.compile(r"!\[[^\]]*\]\(https?://[^)]*\?[^)]*\)"),  # markdown image beacons
    ],
    "override_signals": [
        re.compile(
            r"\b(?:report|say|conclude|mark|set)\b.{0,40}?\b(?:no[_\s]signal|healthy|no\s+incident"
            r"|all\s+(?:good|clear)|root\s+cause\s+is)\b"
        ),
        re.compile(r"\bconfidence\s*[:=]\s*(?:1(?:\.0+)?|100\s*%)"),
    ],
}

KINDS = tuple(_RULES)


def normalize(text: str) -> str:
    """The comparison form: NFKC, invisible characters dropped, homoglyphs folded, lower."""
    folded = unicodedata.normalize("NFKC", text).translate(_INVISIBLE).casefold()
    return folded.translate(_HOMOGLYPHS)


@dataclass(frozen=True)
class InjectionHit:
    kind: str
    excerpt: str  # the matched text (normalised, capped), for the audit log and the report


def scan(text: str, *, extra_tool_names: Iterable[str] = ()) -> list[InjectionHit]:
    """Heuristic hits (at most one per kind) in ``text``. Empty = nothing suspicious.

    ``extra_tool_names`` are write tools of the profile (e.g. ``jira_create_issue``): any
    mention of one in data is a request to call it.
    """
    if not text:
        return []
    norm = normalize(text[:MAX_SCAN_CHARS])
    hits: list[InjectionHit] = []
    for kind, patterns in _RULES.items():
        for pattern in patterns:
            match = pattern.search(norm)
            if match:
                hits.append(InjectionHit(kind, match.group(0)[:120]))
                break
    names = [n.casefold() for n in extra_tool_names if n]
    if names and not any(h.kind == "tool_call_request" for h in hits):
        for name in names:
            if re.search(rf"\b{re.escape(name)}\b", norm):
                hits.append(InjectionHit("tool_call_request", name))
                break
    return hits


#: A <tool_output ...> or </tool_output> tag in any spelling: case, spaces, full-width
#: brackets, invisible characters between the letters.
_ZW = "[\u200b\u200c\u200d\u2060\ufeff\u00ad]*"
_DELIMITER = re.compile(
    r"[<\uff1c\ufe64]" + _ZW + r"\s*" + _ZW + r"/?" + _ZW + r"\s*"
    + _ZW.join("tool_output") + r"(?=[\s>\uff1e\ufe65/\u200b\u200c\u200d\u2060\ufeff]|$)",
    re.IGNORECASE,
)  # fmt: skip


def neutralize_delimiters(text: str) -> str:
    """Escape every spelling of a ``<tool_output>`` tag so data can't close its wrapper."""
    return _DELIMITER.sub(lambda m: "&lt;" + m.group(0)[1:], text)
