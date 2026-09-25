from __future__ import annotations

from pathlib import Path

import pytest

from aiops.core.catalog import CatalogFile, ServiceCatalog, ServiceEntry, normalize
from aiops.core.config import ConfigError, load_settings


@pytest.fixture
def catalog(repo_config_dir: Path) -> ServiceCatalog:
    return ServiceCatalog.from_settings(load_settings("local", repo_config_dir))


@pytest.mark.parametrize(
    ("text", "expected", "match"),
    [
        ("payment-service", "payment-service", "exact"),
        ("Payment_Service", "payment-service", "exact"),
        ("payments api", "payment-service", "alias"),
        ("PAY", "payment-service", "alias"),
        ("checkout", "order-service", "alias"),
        ("login", "user-service", "alias"),
        ("why is inventory slow", "inventory-service", "contains"),
    ],
)
def test_resolve(catalog: ServiceCatalog, text: str, expected: str, match: str) -> None:
    result = catalog.resolve(text)
    assert result.service is not None
    assert result.service.name == expected
    assert result.match == match


def test_unknown_service_is_not_invented(catalog: ServiceCatalog) -> None:
    result = catalog.resolve("billing-engine")
    assert result.service is None
    assert not result.ambiguous


def test_ambiguous_text_returns_candidates(catalog: ServiceCatalog) -> None:
    result = catalog.resolve("payment and inventory are down")
    assert result.service is None
    assert result.ambiguous
    assert result.candidates == ["inventory-service", "payment-service"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("prod", "production"),
        ("Production", "production"),
        ("stg", "staging"),
        ("moon", None),
        (None, None),
    ],
)
def test_resolve_environment(
    catalog: ServiceCatalog, text: str | None, expected: str | None
) -> None:
    assert catalog.resolve_environment(text) == expected


def test_identifiers_merge_environment_overrides(catalog: ServiceCatalog) -> None:
    service = catalog.get("payment-service")
    assert service.identifiers("logs", "production") == {
        "service_value": "payment-service",
        "index_pattern": "payment-prod-*",
    }
    assert service.identifiers("logs") == {"service_value": "payment-service"}
    assert service.identifiers("metrics", "production") == {}


def test_duplicate_alias_rejected() -> None:
    data = CatalogFile(
        services=[
            ServiceEntry(name="a-service", aliases=["shared"]),
            ServiceEntry(name="b-service", aliases=["shared"]),
        ]
    )
    with pytest.raises(ConfigError, match="shared"):
        ServiceCatalog(data)


def test_normalize() -> None:
    assert normalize("  Payments_API.v2 ") == "payments-api-v2"
