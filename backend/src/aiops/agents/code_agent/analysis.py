"""Deterministic change-risk analysis: commits + diffs -> facts, risk scores and signals.

The LLM reasons over these results; it doesn't have to parse diffs or rank commits.

Scoring (0..1) per commit = min(1, sum of rule weights) x proximity x scope weight:
  * rule weights: risky config key changed (+0.2 if the value changed >= 2x), image tag
    changed (+0.3 if no release tag exists for it), dependency bump, schema migration
    (+0.2 if destructive), unbounded in-memory cache, other code; docs/tests score 0;
  * proximity: 1.0 for commits at/after the incident start, decaying linearly to 0.3 at
    the end of the lookback window;
  * scope weight: 1.0 for the service itself, ``DEPENDENCY_WEIGHT`` for a dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

#: Signal vocabulary (must match config/prompts/code/v*.md). All are data-derived.
SIGNALS = (
    "risky_config_change",
    "recent_deployment_change",
    "image_tag_change",
    "unreleased_image_tag",
    "dependency_change",
    "schema_migration",
    "dependency_service_change",
    "no_recent_changes",
)

DEFAULT_RISKY_KEYS = (
    r"POOL|TIMEOUT|MEMORY|CPU|REPLICA|LIMIT|MAX|MIN_|CONN|THREAD|WORKER|RETR|CACHE|HEAP"
    r"|QUEUE|RATE|BATCH|TTL"
)
DEPENDENCY_WEIGHT = 0.7
MIN_PROXIMITY = 0.3

WEIGHTS = {
    "config_key": 0.5,
    "drastic_value": 0.2,
    "image_tag": 0.4,
    "unreleased_image": 0.3,
    "dependency": 0.3,
    "migration": 0.4,
    "destructive_migration": 0.2,
    "unbounded_cache": 0.3,
    "manifest": 0.1,
    "config": 0.1,
    "code": 0.1,
}
#: Pre-score by file category, used to pick which commits get their diff inspected.
PATH_WEIGHTS = {
    "manifest": 0.4,
    "config": 0.4,
    "migration": 0.4,
    "dependency": 0.3,
    "code": 0.1,
    "docs": 0.0,
    "tests": 0.0,
}

DEPENDENCY_FILES = frozenset(
    {
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "poetry.lock",
        "uv.lock",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "go.mod",
        "go.sum",
        "pom.xml",
        "build.gradle",
        "gemfile",
        "gemfile.lock",
        "cargo.toml",
        "cargo.lock",
    }
)
CONFIG_SUFFIXES = (".yaml", ".yml", ".json", ".toml", ".properties", ".env", ".ini", ".conf")
MANIFEST_NAMES = frozenset({"deployment.yaml", "values.yaml", "kustomization.yaml", "chart.yaml"})

_KEY_VALUE = re.compile(
    r"""^(?P<sign>[+-])\s*(?:-\s+)?["']?(?P<key>[A-Za-z_][\w.-]*)["']?\s*[:=]\s*(?P<value>.*?)\s*(?:\#.*)?$"""
)
_IMAGE = re.compile(r"^(?P<image>[^\s\"']+):(?P<tag>[\w][\w.-]*)$")
_REQUIREMENT = re.compile(
    r"^(?P<sign>[+-])\s*(?P<pkg>[A-Za-z0-9_.\-\[\]]+)\s*[=~<>!]=\s*(?P<ver>[\w.\-+]+)"
)
_DESTRUCTIVE_SQL = re.compile(r"\b(DROP|TRUNCATE|ALTER|DELETE\s+FROM)\b", re.I)
_CACHE_ASSIGN = re.compile(r"\b\w*(cache|memo)\w*\b[^=\n]*=\s*(\{\}|dict\(\)|\[\]|list\(\))", re.I)
_EVICTION = re.compile(
    r"(maxsize|max_entries|maxlen|ttl\s*=|lru_cache|\.popitem\(|\.pop\(|\bdel\s)", re.I
)
_QUANTITY = re.compile(r"^(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]*)$")
_UNITS = {
    "": 1.0,
    "m": 1e-3,
    "k": 1e3,
    "ki": 1024.0,
    "mi": 1024.0**2,
    "gi": 1024.0**3,
    "ti": 1024.0**4,
    "g": 1e9,
    "ms": 1e-3,
    "s": 1.0,
    "min": 60.0,
    "h": 3600.0,
}


def classify_path(path: str) -> str:
    p = PurePosixPath(path.lower())
    name = p.name
    parts = set(p.parts)
    if (name.endswith((".md", ".rst", ".txt")) and name not in DEPENDENCY_FILES) or "docs" in parts:
        return "docs"
    if "tests" in parts or "test" in parts or name.startswith("test_") or "_test." in name:
        return "tests"
    if name in DEPENDENCY_FILES:
        return "dependency"
    if "migrations" in parts or "migration" in parts or name.endswith(".sql"):
        return "migration"
    if "k8s" in parts or "helm" in parts or "kustomize" in parts or name in MANIFEST_NAMES:
        return "manifest"
    if "config" in parts or name.endswith(CONFIG_SUFFIXES) or name.startswith(".env"):
        return "config"
    return "code"


def quantity(value: str) -> float | None:
    match = _QUANTITY.match(value.strip().strip("\"'"))
    if not match:
        return None
    unit = _UNITS.get(match.group("unit").lower())
    return None if unit is None else float(match.group("num")) * unit


def proximity(commit_time: datetime, incident_start: datetime, lookback: timedelta) -> float:
    age = (incident_start - commit_time) / lookback
    return 1.0 if age <= 0 else max(MIN_PROXIMITY, 1.0 - age)


@dataclass
class Fact:
    kind: str  # config_key | image_tag | dependency | migration | unbounded_cache | code ...
    file: str
    description: str
    risky: bool = True
    weight: float = 0.0
    hunk: str | None = None


@dataclass
class Commit:
    sha: str
    date: datetime
    author: str
    subject: str
    files: list[str]
    tags: list[str]
    service: str
    dependency: bool  # True if the commit only touches a dependency's paths

    @property
    def short(self) -> str:
        return self.sha[:10]

    @classmethod
    def from_tool(cls, raw: dict[str, Any], service: str, dependency: bool) -> Commit:
        return cls(
            sha=str(raw["sha"]),
            date=datetime.fromisoformat(str(raw["date"]).replace("Z", "+00:00")),
            author=str(raw.get("author", "")),
            subject=str(raw.get("subject", "")),
            files=[str(f["path"]) for f in raw.get("files", [])],
            tags=[str(t) for t in raw.get("tags", [])],
            service=service,
            dependency=dependency,
        )


@dataclass
class CommitRisk:
    commit: Commit
    proximity: float
    scope_weight: float
    categories: list[str]
    facts: list[Fact] = field(default_factory=list)
    inspected: bool = False
    evidence_id: str | None = None

    @property
    def pre_score(self) -> float:
        base = max((PATH_WEIGHTS[c] for c in self.categories), default=0.0)
        return round(base * self.proximity * self.scope_weight, 3)

    @property
    def score(self) -> float:
        if not self.inspected:
            return 0.0
        raw = min(1.0, sum(f.weight for f in self.facts))
        return round(raw * self.proximity * self.scope_weight, 3)

    @property
    def risky_facts(self) -> list[Fact]:
        return [f for f in self.facts if f.risky]

    def headline(self) -> str:
        c = self.commit
        return f"{c.short} '{c.subject}' by {c.author} at {c.date:%Y-%m-%dT%H:%MZ}"

    def fact_summary(self) -> str:
        facts = self.risky_facts or self.facts
        return "; ".join(f.description for f in facts[:4]) or "no risky change"


@dataclass
class Release:
    service: str
    version: str
    tag: str
    sha: str
    date: datetime


def parse_releases(
    raw: list[dict[str, Any]], template: str, services: list[str]
) -> dict[str, list[Release]]:
    """Group release tags by service using the tag template, newest first."""
    out: dict[str, list[Release]] = {s: [] for s in services}
    prefix, _, suffix = template.partition("{version}")
    for item in raw:
        tag = str(item.get("tag", ""))
        for service in services:
            head = prefix.format(service=service)
            tail = suffix.format(service=service)
            if tag.startswith(head) and tag.endswith(tail) and len(tag) > len(head) + len(tail):
                version = tag[len(head) : len(tag) - len(tail)] if tail else tag[len(head) :]
                out[service].append(
                    Release(
                        service=service,
                        version=version,
                        tag=tag,
                        sha=str(item.get("sha", "")),
                        date=datetime.fromisoformat(str(item["date"]).replace("Z", "+00:00")),
                    )
                )
    for releases in out.values():
        releases.sort(key=lambda r: r.date, reverse=True)
    return out


# --------------------------------------------------------------------------- diff rules


def _pairs(hunk: str) -> dict[str, tuple[str | None, str | None]]:
    """key -> (old value, new value) for key/value lines changed in a hunk."""
    pairs: dict[str, tuple[str | None, str | None]] = {}
    for line in hunk.splitlines():
        if line.startswith(("+++", "---")):
            continue
        match = _KEY_VALUE.match(line)
        if not match:
            continue
        key = match.group("key")
        value = match.group("value").strip().strip("\"'")
        old, new = pairs.get(key, (None, None))
        pairs[key] = (value, new) if match.group("sign") == "-" else (old, value)
    return {k: v for k, v in pairs.items() if v[0] != v[1]}


def _drastic(old: str | None, new: str | None) -> bool:
    a, b = quantity(old or ""), quantity(new or "")
    if a is None or b is None or a <= 0 or b <= 0:
        return False
    return max(a, b) / min(a, b) >= 2.0


class DiffRules:
    def __init__(self, risky_keys: str = DEFAULT_RISKY_KEYS) -> None:
        self.risky_key = re.compile(risky_keys, re.I)

    def facts(
        self, files: list[dict[str, Any]], *, service: str, release_tags: set[str], template: str
    ) -> list[Fact]:
        facts: list[Fact] = []
        for f in files:
            path = str(f.get("path", ""))
            category = classify_path(path)
            hunks = [str(h) for h in f.get("hunks", [])]
            before = len(facts)
            if category in ("config", "manifest"):
                facts += self._config(path, hunks, service, release_tags, template)
            elif category == "dependency":
                facts += self._dependency(path, hunks)
            elif category == "migration":
                facts += self._migration(path, hunks)
            elif category == "code":
                facts += self._code(path, hunks)
            if len(facts) == before and category not in ("docs", "tests"):
                facts.append(
                    Fact(
                        category,
                        path,
                        f"{category} change in {path}",
                        risky=False,
                        weight=WEIGHTS.get(category, 0.0),
                        hunk=hunks[0] if hunks else None,
                    )
                )
        return facts

    def _config(
        self, path: str, hunks: list[str], service: str, release_tags: set[str], template: str
    ) -> list[Fact]:
        out: list[Fact] = []
        for hunk in hunks:
            for key, (old, new) in _pairs(hunk).items():
                if key == "image":
                    old_tag = _IMAGE.match(old or "")
                    new_tag = _IMAGE.match(new or "")
                    if not new_tag:
                        continue
                    tag = new_tag.group("tag")
                    released = template.format(service=service, version=tag) in release_tags
                    desc = (
                        f"image tag {old_tag.group('tag') if old_tag else old} -> {tag} in {path}"
                    )
                    if not released:
                        desc += f" (no release tag {template.format(service=service, version=tag)})"
                    out.append(
                        Fact("image_tag", path, desc, weight=WEIGHTS["image_tag"], hunk=hunk)
                    )
                    if not released:
                        out.append(
                            Fact(
                                "unreleased_image",
                                path,
                                f"image tag {tag} has no matching release",
                                weight=WEIGHTS["unreleased_image"],
                            )
                        )
                elif self.risky_key.search(key):
                    change = (
                        f"{key} {old!r} -> {new!r}" if old is not None else f"{key} added = {new!r}"
                    )
                    drastic = _drastic(old, new)
                    weight = WEIGHTS["config_key"] + (WEIGHTS["drastic_value"] if drastic else 0.0)
                    desc = f"{change} in {path}" + (" (changed >= 2x)" if drastic else "")
                    out.append(Fact("config_key", path, desc, weight=weight, hunk=hunk))
        return out

    def _dependency(self, path: str, hunks: list[str]) -> list[Fact]:
        changes: dict[str, list[str | None]] = {}
        for hunk in hunks:
            for line in hunk.splitlines():
                match = _REQUIREMENT.match(line)
                if match:
                    old_new = changes.setdefault(match.group("pkg"), [None, None])
                    old_new[0 if match.group("sign") == "-" else 1] = match.group("ver")
        if not changes:
            return []
        desc = ", ".join(f"{pkg} {old} -> {new}" for pkg, (old, new) in changes.items())
        return [
            Fact(
                "dependency",
                path,
                f"dependency change in {path}: {desc}",
                weight=WEIGHTS["dependency"],
                hunk=hunks[0] if hunks else None,
            )
        ]

    def _migration(self, path: str, hunks: list[str]) -> list[Fact]:
        added = "\n".join(
            line[1:] for h in hunks for line in h.splitlines() if line.startswith("+")
        )
        destructive = _DESTRUCTIVE_SQL.search(added)
        weight = WEIGHTS["migration"] + (WEIGHTS["destructive_migration"] if destructive else 0.0)
        desc = f"schema migration {path}" + (
            f" ({destructive.group(1).upper()} statement)" if destructive else ""
        )
        return [Fact("migration", path, desc, weight=weight, hunk=hunks[0] if hunks else None)]

    def _code(self, path: str, hunks: list[str]) -> list[Fact]:
        body = "\n".join(hunks)
        added = [line[1:] for h in hunks for line in h.splitlines() if line.startswith("+")]
        cache = next((line for line in added if _CACHE_ASSIGN.search(line)), None)
        out = [
            Fact(
                "code",
                path,
                f"code change in {path}",
                risky=False,
                weight=WEIGHTS["code"],
                hunk=hunks[0] if hunks else None,
            )
        ]
        if cache and not _EVICTION.search(body):
            hunk = next((h for h in hunks if cache in h), None)
            out.append(
                Fact(
                    "unbounded_cache",
                    path,
                    f"new in-memory cache without eviction in {path}: `{cache.strip()}`",
                    weight=WEIGHTS["unbounded_cache"],
                    hunk=hunk,
                )
            )
        return out


# --------------------------------------------------------------------------- analysis


@dataclass
class CodeAnalysis:
    service: str
    since: datetime
    until: datetime
    incident_start: datetime
    paths: dict[str, list[str]]  # service -> paths scanned
    risks: list[CommitRisk]
    releases: dict[str, list[Release]]
    threshold: float
    signals: list[str] = field(default_factory=list)

    @property
    def suspects(self) -> list[CommitRisk]:
        ranked = [r for r in self.risks if r.score >= self.threshold]
        return sorted(ranked, key=lambda r: (-r.score, -r.commit.date.timestamp()))

    @property
    def recent_releases(self) -> list[Release]:
        return [
            r for r in self.releases.get(self.service, []) if self.since <= r.date <= self.until
        ]

    @property
    def has_findings(self) -> bool:
        return bool(self.suspects) or any(s != "no_recent_changes" for s in self.signals)

    def compute_signals(self) -> list[str]:
        found: set[str] = set()
        for risk in self.suspects:
            kinds = {f.kind for f in risk.risky_facts}
            if risk.commit.dependency:
                found.add("dependency_service_change")
                continue
            if "config_key" in kinds:
                found.add("risky_config_change")
            if "image_tag" in kinds:
                found.add("image_tag_change")
            if "unreleased_image" in kinds:
                found.add("unreleased_image_tag")
            if "dependency" in kinds:
                found.add("dependency_change")
            if "migration" in kinds:
                found.add("schema_migration")
            if any(classify_path(f.file) == "manifest" for f in risk.facts):
                found.add("recent_deployment_change")
        if self.recent_releases:
            found.add("recent_deployment_change")
        if not found and not self.suspects:
            found = {"no_recent_changes"}
        self.signals = [s for s in SIGNALS if s in found]
        return self.signals

    def lines(self) -> list[str]:
        """Compact text for the LLM prompt."""
        scanned = "; ".join(f"{s}: {', '.join(p)}" for s, p in self.paths.items())
        out = [
            f"Scanned {len(self.risks)} commits in [{self.since:%Y-%m-%dT%H:%MZ}, "
            f"{self.until:%Y-%m-%dT%H:%MZ}] touching {scanned}. Incident start: "
            f"{self.incident_start:%Y-%m-%dT%H:%MZ}.",
        ]
        for service, releases in self.releases.items():
            if releases:
                latest = ", ".join(f"{r.version} ({r.date:%Y-%m-%d %H:%MZ})" for r in releases[:2])
                out.append(f"Latest releases of {service}: {latest}")
        if self.recent_releases:
            out.append(
                "Released in the scan window: "
                + ", ".join(f"{r.tag} at {r.date:%Y-%m-%dT%H:%MZ}" for r in self.recent_releases)
            )
        suspects = self.suspects
        if suspects:
            out.append("Suspect commits (risk score >= threshold, highest first):")
            for risk in suspects:
                scope = (
                    f"dependency {risk.commit.service}"
                    if risk.commit.dependency
                    else risk.commit.service
                )
                out.append(
                    f"- [{risk.evidence_id}] {risk.headline()} [{scope}] risk={risk.score:.2f}: "
                    f"{risk.fact_summary()}"
                )
                for fact in risk.risky_facts[:2]:
                    if fact.hunk:
                        out.append(f"  hunk ({fact.file}):\n" + _indent(fact.hunk, 800))
        else:
            out.append("No suspect commits: no risky code/config change in the scan window.")
        others = [r for r in self.risks if r not in suspects]
        if others:
            out.append("Other commits (low risk):")
            for risk in others[:12]:
                note = (
                    risk.fact_summary()
                    if risk.inspected
                    else "not inspected (docs/tests/code by path)"
                )
                ref = f"[{risk.evidence_id}] " if risk.evidence_id else ""
                out.append(f"- {ref}{risk.headline()} [{risk.commit.service}]: {note}")
        out.append("Signals (deterministic): " + (", ".join(self.signals) or "none"))
        return out


def _indent(text: str, limit: int) -> str:
    clipped = text if len(text) <= limit else text[:limit] + "\n...[truncated]"
    return "\n".join(f"    {line}" for line in clipped.splitlines())
