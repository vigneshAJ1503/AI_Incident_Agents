"""Markdown runbooks -> front matter + heading-aware sections and chunks.

    # Database connection pool exhaustion        (H1 = document title)
    ## Diagnosis
    ### 1. Confirm the pool is the bottleneck    -> heading path
       "Database connection pool exhaustion > Diagnosis > 1. Confirm the pool is the bottleneck"

A chunk is one section's own text (up to the next heading of any level), split
at paragraph boundaries when it is long. Every chunk keeps its heading path, so a
search hit can be cited as "doc > section". Dependency-free and deterministic;
shared by ingestion and the Knowledge agent (section extraction).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

#: Bump when chunking changes: it is part of the content hash, so every doc is re-ingested.
CHUNKER_VERSION = "1"
MAX_CHUNK_CHARS = 1_500

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_ANCHOR_DROP = re.compile(r"[^\w\- ]", re.UNICODE)
_BLANK_LINES = re.compile(r"\n\s*\n")


class FrontMatterError(ValueError):
    """The YAML front matter is malformed."""


@dataclass(frozen=True)
class Section:
    heading: str  # "" for text before the first heading
    level: int  # 1-6; 0 for the preamble
    path: tuple[str, ...]  # heading titles from the H1 down to this heading
    text: str  # the section's own body (without sub-sections)
    anchor: str  # GitHub-style anchor, e.g. "1-confirm-the-pool-is-the-bottleneck"

    @property
    def heading_path(self) -> str:
        return " > ".join(self.path)


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    heading: str
    heading_path: str
    anchor: str
    content: str


@dataclass(frozen=True)
class Document:
    path: str
    title: str
    doc_type: str
    services: tuple[str, ...]
    tags: tuple[str, ...]
    metadata: dict[str, Any] = field(hash=False)
    body: str = field(hash=False)
    sections: tuple[Section, ...] = field(hash=False)


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """'---\\nyaml\\n---\\nbody' -> (metadata, body). No front matter -> ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if lines[0].strip() != "---":
        return {}, text
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() in ("---", "..."):
            raw = "".join(lines[1:i])
            try:
                data = yaml.safe_load(raw) or {}
            except yaml.YAMLError as exc:
                raise FrontMatterError(f"invalid YAML front matter: {exc}") from exc
            if not isinstance(data, dict):
                raise FrontMatterError("front matter must be a mapping")
            return data, "".join(lines[i + 1 :])
    raise FrontMatterError("front matter is not closed with '---'")


def github_anchor(heading: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces -> '-'."""
    text = heading.strip().lower()
    text = re.sub(r"`", "", text)
    return _ANCHOR_DROP.sub("", text).replace(" ", "-")


def parse_sections(body: str) -> list[Section]:
    """Split markdown into sections by ATX headings (ignoring headings inside code fences)."""
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []  # (level, title)
    anchors: dict[str, int] = {}
    heading, level, anchor = "", 0, ""
    buffer: list[str] = []
    in_fence = False

    def flush() -> None:
        sections.append(
            Section(
                heading=heading,
                level=level,
                path=tuple(title for _, title in stack),
                text="\n".join(buffer).strip(),
                anchor=anchor,
            )
        )

    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        match = None if in_fence else _HEADING.match(line)
        if match is None:
            buffer.append(line)
            continue
        if heading or "".join(buffer).strip():
            flush()
        level, heading = len(match.group(1)), match.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading))
        base = github_anchor(heading)
        seen = anchors.get(base, 0)
        anchors[base] = seen + 1
        anchor = base if seen == 0 else f"{base}-{seen}"
        buffer = []
    if heading or "".join(buffer).strip():
        flush()
    return sections


def _as_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(v) for v in value if v is not None and str(v).strip())
    raise FrontMatterError(f"expected a list, got {type(value).__name__}")


def parse_document(path: str, text: str) -> Document:
    metadata, body = split_front_matter(text)
    sections = parse_sections(body)
    h1 = next((s.heading for s in sections if s.level == 1), None)
    stem = path.rsplit("/", 1)[-1].removesuffix(".md")
    return Document(
        path=path,
        title=str(metadata.get("title") or h1 or stem),
        doc_type=str(metadata.get("type") or "doc"),
        services=_as_list(metadata.get("services")),
        tags=_as_list(metadata.get("tags")),
        metadata=metadata,
        body=body,
        sections=tuple(sections),
    )


def _split_long(text: str, limit: int) -> list[str]:
    """Split at blank lines into pieces of at most ~limit chars (a huge paragraph stays whole)."""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    current = ""
    for paragraph in _BLANK_LINES.split(text):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if current and len(candidate) > limit:
            pieces.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def chunk_document(doc: Document, max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    """One chunk per non-empty section (split when long); heading-only sections are skipped."""
    chunks: list[Chunk] = []
    for section in doc.sections:
        if not section.text:
            continue
        path = section.heading_path or doc.title
        for piece in _split_long(section.text, max_chars):
            chunks.append(
                Chunk(
                    ordinal=len(chunks),
                    heading=section.heading or doc.title,
                    heading_path=path,
                    anchor=section.anchor,
                    content=piece,
                )
            )
    return chunks
