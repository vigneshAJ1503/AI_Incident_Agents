"""Day-1 onboarding tools live, READ-ONLY (PR-P3): `aiops doctor` and `aiops catalog import`.

Requires the local stack (make infra-up mcp-up k8s-up logging-up). Nothing is written:
doctor only calls allowlisted read tools, and the import runs with --dry-run.
Run with: make test-integration
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from aiops.cli.main import app
from aiops.core.catalog_import import plan_import, resolved_catalog_data
from aiops.core.config import load_settings_lenient
from aiops.core.doctor import Doctor, DoctorOptions, Status

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "config"
SAMPLE_SERVICES = {"payment-service", "order-service", "user-service", "inventory-service"}


@pytest.fixture(autouse=True)
def _repo_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOPS_CONFIG_DIR", str(CONFIG))


@pytest.mark.parametrize("profile", ["local", "local-k8s"])
async def test_doctor_is_green_on_the_local_stack(profile: str) -> None:
    settings, missing = load_settings_lenient(profile, CONFIG, keep_missing=True)
    report = await Doctor(
        settings, DoctorOptions(skip_llm=True, sample=2), missing_vars=missing
    ).run()
    failures = [c for c in report.checks if c.status == Status.FAIL]
    assert failures == [], failures
    capabilities = {c.capability for c in report.checks if c.check == "reachability"}
    assert capabilities == {"logs", "metrics", "alerts", "k8s", "code", "tickets", "knowledge"}
    assert all(
        c.status == Status.OK for c in report.checks if c.check in ("reachability", "contract")
    )
    catalog = [c for c in report.checks if c.check == "catalog"]
    assert any(c.capability == "k8s" and c.status == Status.OK for c in catalog)
    if profile == "local-k8s":
        # Real Fluent Bit logs: the shared index filtered on the app label has recent docs.
        logs = [c for c in catalog if c.capability == "logs"]
        assert logs and all(c.status == Status.OK for c in logs), logs


def test_catalog_import_from_minikube_matches_the_hand_written_catalog() -> None:
    result = CliRunner().invoke(
        app,
        [
            "catalog",
            "import",
            "--from",
            "kubernetes",
            "--profile",
            "local-k8s",
            "--namespace",
            "prod",
            "--selector",
            "team!=platform",
            "--dry-run",
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.output
    assert "4 services found: 0 new, 0 updated, 4 unchanged, 0 hand-edited values kept" in (
        result.output
    )
    for name in SAMPLE_SERVICES:
        assert f"service {name} (unchanged)" in result.output


def test_catalog_import_generates_the_sample_catalog_from_scratch(tmp_path: Path) -> None:
    out = tmp_path / "services.yaml"
    result = CliRunner().invoke(
        app,
        [
            "catalog",
            "import",
            "--from",
            "kubernetes",
            "--profile",
            "local-k8s",
            "--namespace",
            "prod",
            "--selector",
            "team!=platform",
            "--output",
            str(out),
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.output
    generated = yaml.safe_load(out.read_text())
    assert {s["name"] for s in generated["services"]} == SAMPLE_SERVICES
    # Everything generated is already in the hand-written catalog, value for value.
    hand_written = resolved_catalog_data(ROOT / "profiles" / "local-k8s" / "services.yaml")
    plan = plan_import(hand_written, generated["services"])
    assert [c.kind for c in plan.changes] == ["unchanged"] * 4
    assert plan.conflicts == 0
