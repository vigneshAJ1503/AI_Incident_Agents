from __future__ import annotations

from pathlib import Path

import pytest

from aiops.knowledge.ingest import IngestError, content_hash, plan_changes, scan
from aiops.knowledge.markdown import (
    FrontMatterError,
    chunk_document,
    github_anchor,
    parse_document,
    parse_sections,
    split_front_matter,
)

RUNBOOK = """---
title: DB pool
type: runbook
services: [payment-service]
tags: [database]
last_reviewed: 2026-09-01
---
# Database connection pool exhaustion

Intro text.

## Symptoms
- `could not acquire a connection`

## Diagnosis
### 1. Confirm the pool
Check saturation.

```bash
# not a heading
kubectl get pods
```

## Diagnosis
Second section with the same name.
"""


def test_front_matter_split() -> None:
    meta, body = split_front_matter(RUNBOOK)
    assert meta["services"] == ["payment-service"] and meta["type"] == "runbook"
    assert body.startswith("# Database connection pool exhaustion")
    assert split_front_matter("# no front matter") == ({}, "# no front matter")
    with pytest.raises(FrontMatterError, match="not closed"):
        split_front_matter("---\ntitle: x\n# body")
    with pytest.raises(FrontMatterError, match="mapping"):
        split_front_matter("---\n- a\n---\n")


def test_sections_keep_heading_paths_and_ignore_code_fences() -> None:
    sections = parse_sections(split_front_matter(RUNBOOK)[1])
    paths = [s.heading_path for s in sections]
    assert paths == [
        "Database connection pool exhaustion",
        "Database connection pool exhaustion > Symptoms",
        "Database connection pool exhaustion > Diagnosis",
        "Database connection pool exhaustion > Diagnosis > 1. Confirm the pool",
        "Database connection pool exhaustion > Diagnosis",
    ]
    confirm = sections[3]
    assert "# not a heading" in confirm.text and confirm.level == 3
    assert confirm.anchor == "1-confirm-the-pool"
    assert [s.anchor for s in sections if s.heading == "Diagnosis"] == ["diagnosis", "diagnosis-1"]


def test_github_anchor() -> None:
    assert github_anchor("High latency (p95/p99)") == "high-latency-p95p99"
    assert github_anchor("2. Leak or undersized `limit`?") == "2-leak-or-undersized-limit"


def test_document_and_chunks() -> None:
    doc = parse_document("kb/runbooks/db.md", RUNBOOK)
    assert (doc.title, doc.doc_type, doc.services, doc.tags) == (
        "DB pool",
        "runbook",
        ("payment-service",),
        ("database",),
    )
    chunks = chunk_document(doc)
    # the empty "## Diagnosis" parent is skipped; its child keeps the full path
    assert [c.heading for c in chunks] == [
        "Database connection pool exhaustion",
        "Symptoms",
        "1. Confirm the pool",
        "Diagnosis",
    ]
    assert [c.ordinal for c in chunks] == [0, 1, 2, 3]
    untitled = parse_document("kb/x.md", "just text")
    assert untitled.title == "x" and untitled.doc_type == "doc"
    assert chunk_document(untitled)[0].heading_path == "x"


def test_long_sections_split_at_paragraphs() -> None:
    body = "# T\n\n## Long\n" + "\n\n".join(f"paragraph {i} " + "word " * 40 for i in range(10))
    chunks = chunk_document(parse_document("kb/t.md", body), max_chars=500)
    assert len(chunks) > 3
    assert all(len(c.content) <= 500 for c in chunks)
    assert {c.heading_path for c in chunks} == {"T > Long"}
    assert "paragraph 0" in chunks[0].content and "paragraph 9" in chunks[-1].content


def test_plan_changes() -> None:
    plan = plan_changes({"a": "1", "b": "2", "c": "3"}, {"a": "1", "b": "X", "d": "4"})
    assert (plan.add, plan.update, plan.unchanged, plan.delete) == (["d"], ["b"], ["a"], ["c"])


def test_scan_prefixes_paths_and_skips_readme(tmp_path: Path) -> None:
    (tmp_path / "runbooks").mkdir()
    (tmp_path / "runbooks" / "a.md").write_text(RUNBOOK)
    (tmp_path / "README.md").write_text("# readme")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "b.md").write_text("# hidden")
    found = scan(tmp_path, "knowledge-base")
    assert list(found) == ["knowledge-base/runbooks/a.md"]
    assert found["knowledge-base/runbooks/a.md"].hash == content_hash(RUNBOOK)
    (tmp_path / "runbooks" / "bad.md").write_text("---\n[\n---\n")
    with pytest.raises(IngestError, match=r"bad\.md"):
        scan(tmp_path, "knowledge-base")
    with pytest.raises(IngestError, match="not found"):
        scan(tmp_path / "missing", "kb")
