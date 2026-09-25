"""Lint the real knowledge base: it is data the agents depend on."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiops.core.catalog import ServiceCatalog
from aiops.core.config import load_settings
from aiops.knowledge.ingest import scan

REPO = Path(__file__).resolve().parents[3]
DOCS = scan(REPO / "knowledge-base", "knowledge-base")
RUNBOOK_SECTIONS = {"Summary", "Symptoms", "Diagnosis", "Mitigation", "Escalation"}
CATALOG = ServiceCatalog.from_settings(load_settings("local", REPO / "config"))


def test_knowledge_base_has_runbooks_and_service_docs() -> None:
    types = [d.document.doc_type for d in DOCS.values()]
    assert 8 <= types.count("runbook") <= 12
    assert types.count("service") == len(CATALOG.services)
    assert set(types) <= {"runbook", "service"}


@pytest.mark.parametrize(
    "path", [p for p, d in DOCS.items() if d.document.doc_type == "runbook"], ids=lambda p: p
)
def test_runbooks_have_the_standard_sections(path: str) -> None:
    doc = DOCS[path].document
    h2 = {s.heading for s in doc.sections if s.level == 2}
    assert h2 >= RUNBOOK_SECTIONS, f"missing {sorted(RUNBOOK_SECTIONS - h2)}"
    assert doc.metadata.get("owner") and doc.tags


def test_front_matter_services_exist_in_the_catalog() -> None:
    known = {s.name for s in CATALOG.services}
    for path, source in DOCS.items():
        unknown = set(source.document.services) - known
        assert not unknown, f"{path}: unknown services {unknown}"


def test_catalog_runbooks_resolve_to_files() -> None:
    names = {p.rsplit("/", 1)[1] for p in DOCS}
    for service in CATALOG.services:
        missing = set(service.runbooks) - names
        assert not missing, f"{service.name}: {missing} not in knowledge-base/"


def test_relative_links_point_to_existing_docs() -> None:
    import re

    link = re.compile(r"\]\(([^)#]+\.md)\)")
    for path, source in DOCS.items():
        base = (REPO / path).parent
        for target in link.findall(source.document.body):
            assert (base / target).resolve().is_file(), f"{path}: broken link {target}"


def test_alert_runbook_urls_resolve() -> None:
    """Alert rules (and the alert seeder) link runbooks by repo-relative path."""
    import re

    from aiops.seed.alerts import RULES

    rules = (REPO / "deploy/compose/config/prometheus/alert-rules.yml").read_text()
    urls = set(re.findall(r"runbook_url:\s*(\S+\.md)", rules))
    urls |= {rule.runbook_url for rule in RULES.values()}
    assert urls, "no runbook_url found in alert rules"
    missing = sorted(u for u in urls if u not in DOCS)
    assert not missing, f"alert runbook_url(s) without a runbook: {missing}"
