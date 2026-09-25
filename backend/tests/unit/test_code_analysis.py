from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aiops.agents.code_agent.analysis import (
    CodeAnalysis,
    Commit,
    CommitRisk,
    DiffRules,
    classify_path,
    parse_releases,
    proximity,
    quantity,
)

START = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
DAY = timedelta(hours=24)
TEMPLATE = "{service}/{version}"


@pytest.mark.parametrize(
    ("path", "category"),
    [
        ("services/p/config/app.yaml", "config"),
        ("services/p/k8s/deployment.yaml", "manifest"),
        ("charts/p/values.yaml", "manifest"),
        ("services/p/requirements.txt", "dependency"),
        ("package.json", "dependency"),
        ("services/p/migrations/0003_drop.sql", "migration"),
        ("services/p/README.md", "docs"),
        ("services/p/docs/architecture.md", "docs"),
        ("services/p/tests/test_orders.py", "tests"),
        ("services/p/app/main.py", "code"),
        (".env.production", "config"),
    ],
)
def test_classify_path(path: str, category: str) -> None:
    assert classify_path(path) == category


def test_quantity_units() -> None:
    assert quantity("1Gi") == 2 * quantity("512Mi")  # type: ignore[operator]
    assert quantity('"20"') == 20
    assert quantity("250m") == 0.25
    assert quantity("abc") is None and quantity("5 parsecs") is None


def test_proximity() -> None:
    assert proximity(START + timedelta(minutes=5), START, DAY) == 1.0
    assert proximity(START - timedelta(hours=12), START, DAY) == pytest.approx(0.5)
    assert proximity(START - timedelta(hours=23), START, DAY) == 0.3


def hunk(*lines: str) -> str:
    return "@@ -1,3 +1,3 @@\n" + "\n".join(lines)


def facts(path: str, *hunks: str, service: str = "p", tags: set[str] | None = None) -> list:  # type: ignore[type-arg]
    return DiffRules().facts(
        [{"path": path, "hunks": list(hunks)}],
        service=service,
        release_tags=tags or set(),
        template=TEMPLATE,
    )


def test_config_key_change_drastic() -> None:
    (fact,) = facts(
        "s/config/app.yaml", hunk(" env:", '-  DB_POOL_SIZE: "20"', '+  DB_POOL_SIZE: "2"')
    )
    assert fact.kind == "config_key" and fact.risky
    assert fact.description == "DB_POOL_SIZE '20' -> '2' in s/config/app.yaml (changed >= 2x)"
    assert fact.weight == pytest.approx(0.7)


def test_non_risky_config_key_is_low_weight() -> None:
    (fact,) = facts("s/config/app.yaml", hunk('-  DB_HOST: "a"', '+  DB_HOST: "b"'))
    assert fact.kind == "config" and not fact.risky and fact.weight == 0.1


def test_added_risky_key_and_memory_limit() -> None:
    added = facts("s/config/app.yaml", hunk('+  ORDER_CACHE_ENABLED: "true"'))
    assert added[0].description.startswith("ORDER_CACHE_ENABLED added = 'true'")
    (limit,) = facts("s/k8s/deployment.yaml", hunk("-      memory: 1Gi", "+      memory: 512Mi"))
    assert limit.kind == "config_key" and "(changed >= 2x)" in limit.description


def test_image_tag_released_vs_unreleased() -> None:
    change = hunk("-  image: reg/acme/p:v1.0.0", "+  image: reg/acme/p:v1.1.0")
    released = facts("s/k8s/deployment.yaml", change, tags={"p/v1.1.0"})
    assert [f.kind for f in released] == ["image_tag"]
    unreleased = facts("s/k8s/deployment.yaml", change, tags={"p/v1.0.0"})
    assert [f.kind for f in unreleased] == ["image_tag", "unreleased_image"]
    assert "(no release tag p/v1.1.0)" in unreleased[0].description


def test_dependency_migration_and_cache_rules() -> None:
    (dep,) = facts("s/requirements.txt", hunk("-httpx==0.27.0", "+httpx==0.28.1"))
    assert dep.description.endswith("httpx 0.27.0 -> 0.28.1")
    (mig,) = facts("s/migrations/3.sql", "@@ -0,0 +1 @@\n+DROP INDEX IF EXISTS idx;")
    assert mig.kind == "migration" and "(DROP statement)" in mig.description
    assert mig.weight == pytest.approx(0.6)
    code = facts("s/app/cache.py", "@@ -0,0 +2 @@\n+_CACHE: dict[str, int] = {}\n+# no eviction")
    assert [f.kind for f in code] == ["code", "unbounded_cache"]
    bounded = facts("s/app/cache.py", "@@ -0,0 +2 @@\n+_cache = {}\n+_cache.popitem()")
    assert [f.kind for f in bounded] == ["code"]
    assert facts("s/README.md", hunk("-a", "+b")) == []


def commit(subject: str, hours_before: float, *, dependency: bool = False) -> Commit:
    return Commit(
        sha=subject.replace(" ", "")[:10].ljust(40, "0"),
        date=START - timedelta(hours=hours_before),
        author="Dev",
        subject=subject,
        files=[],
        tags=[],
        service="dep" if dependency else "p",
        dependency=dependency,
    )


def risk(c: Commit, fact_list: list, scope_weight: float = 1.0) -> CommitRisk:  # type: ignore[type-arg]
    r = CommitRisk(c, proximity(c.date, START, DAY), scope_weight, categories=["config"])
    r.facts, r.inspected = fact_list, True
    return r


def analysis(*risks: CommitRisk, releases: dict | None = None) -> CodeAnalysis:  # type: ignore[type-arg]
    a = CodeAnalysis(
        service="p",
        since=START - DAY,
        until=START + timedelta(minutes=30),
        incident_start=START,
        paths={"p": ["services/p"]},
        risks=list(risks),
        releases=releases or {},
        threshold=0.3,
    )
    a.compute_signals()
    return a


def test_scores_rank_and_signals() -> None:
    pool = risk(
        commit("tune db pool", 2),
        facts("s/config/app.yaml", hunk('-  DB_POOL_SIZE: "20"', '+  DB_POOL_SIZE: "2"')),
    )
    docs = risk(commit("docs", 1), [])
    old = risk(  # same change a day ago: decayed below the threshold
        commit("old pool change", 23),
        facts("s/config/app.yaml", hunk('-  DB_POOL_SIZE: "20"', '+  DB_POOL_SIZE: "21"')),
    )
    a = analysis(pool, docs, old)
    assert [r.commit.subject for r in a.suspects] == ["tune db pool"]
    assert pool.score == pytest.approx(0.7 * (1 - 2 / 24), abs=1e-3)
    assert a.signals == ["risky_config_change"] and a.has_findings


def test_no_suspects_means_no_recent_changes() -> None:
    a = analysis(risk(commit("docs", 1), []))
    assert a.signals == ["no_recent_changes"] and not a.has_findings
    assert "No suspect commits" in "\n".join(a.lines())


def test_dependency_suspect_and_recent_release() -> None:
    mig = risk(
        commit("drop index", 1, dependency=True),
        facts("s/migrations/3.sql", "@@ -0,0 +1 @@\n+DROP INDEX i;"),
        scope_weight=0.7,
    )
    releases = parse_releases(
        [{"tag": "p/v2.0.0", "sha": "x", "date": "2026-09-25T09:00:00Z"}], TEMPLATE, ["p", "q"]
    )
    a = analysis(mig, releases=releases)
    assert a.signals == ["recent_deployment_change", "dependency_service_change"]
    assert releases["p"][0].version == "v2.0.0" and releases["q"] == []
