"""Service Catalog: maps a service name/alias to its identifiers in every capability.

Example: "payments api" in "prod" -> payment-service / production ->
``logs.index_pattern = payment-prod-*``, ``k8s.namespace = prod`` ...
The planner must never invent a service that is not in the catalog (UC-13).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aiops.core.config import ConfigError, Settings, format_validation_error, load_yaml

_SEPARATORS = re.compile(r"[\s_./]+")


def normalize(text: str) -> str:
    """'Payments_API ' -> 'payments-api'."""
    return _SEPARATORS.sub("-", text.strip().casefold()).strip("-")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EnvironmentEntry(_Model):
    aliases: list[str] = Field(default_factory=list)
    description: str = ""


class ServiceEnvironment(_Model):
    """Per-environment overrides of capability identifiers."""

    capabilities: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ServiceEntry(_Model):
    name: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    owners: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    runbooks: list[str] = Field(default_factory=list)
    capabilities: dict[str, dict[str, Any]] = Field(default_factory=dict)
    environments: dict[str, ServiceEnvironment] = Field(default_factory=dict)

    def identifiers(self, capability: str, environment: str | None = None) -> dict[str, Any]:
        """Capability identifiers: service defaults merged with environment overrides."""
        merged = dict(self.capabilities.get(capability, {}))
        if environment and environment in self.environments:
            merged.update(self.environments[environment].capabilities.get(capability, {}))
        return merged

    def names(self) -> set[str]:
        return {normalize(self.name), *(normalize(a) for a in self.aliases)}


class CatalogFile(_Model):
    environments: dict[str, EnvironmentEntry] = Field(default_factory=dict)
    services: list[ServiceEntry] = Field(default_factory=list)


class ServiceResolution(_Model):
    """Result of resolving free text to a service."""

    query: str
    service: ServiceEntry | None = None
    candidates: list[str] = Field(default_factory=list)
    match: str = "none"  # exact | alias | contains | none

    @property
    def ambiguous(self) -> bool:
        return self.service is None and len(self.candidates) > 1


class ServiceCatalog:
    def __init__(self, data: CatalogFile) -> None:
        self._environments = data.environments
        self._services: dict[str, ServiceEntry] = {}
        seen: dict[str, str] = {}
        for service in data.services:
            if service.name in self._services:
                raise ConfigError(f"Duplicate service in catalog: {service.name}")
            for name in service.names():
                if name in seen and seen[name] != service.name:
                    raise ConfigError(
                        f"Alias '{name}' is used by both '{seen[name]}' and '{service.name}'."
                    )
                seen[name] = service.name
            self._services[service.name] = service
        self._by_name = seen

    # -- loading -------------------------------------------------------------------------

    @classmethod
    def from_file(cls, path: Path) -> ServiceCatalog:
        try:
            data = CatalogFile.model_validate(load_yaml(path))
        except ValidationError as err:
            raise ConfigError(format_validation_error(err, path)) from err
        return cls(data)

    @classmethod
    def from_settings(cls, settings: Settings) -> ServiceCatalog:
        path = settings.config_dir / "service-catalog" / f"{settings.service_catalog}.yaml"
        return cls.from_file(path)

    # -- queries -------------------------------------------------------------------------

    @property
    def services(self) -> list[ServiceEntry]:
        return list(self._services.values())

    def get(self, name: str) -> ServiceEntry:
        try:
            return self._services[name]
        except KeyError:
            raise ConfigError(f"Unknown service '{name}'.") from None

    def resolve(self, text: str) -> ServiceResolution:
        """Resolve free text ('payments api', 'Payment-Service') to a catalog service."""
        query = normalize(text)
        if not query:
            return ServiceResolution(query=text)
        if query in self._services:
            return ServiceResolution(query=text, service=self._services[query], match="exact")
        if query in self._by_name:
            service = self._services[self._by_name[query]]
            return ServiceResolution(query=text, service=service, match="alias")

        # Word-boundary containment either way: "payments-api" contains alias "payments".
        hits: set[str] = set()
        padded_query = f"-{query}-"
        for name, service_name in self._by_name.items():
            if f"-{name}-" in padded_query or f"-{query}-" in f"-{name}-":
                hits.add(service_name)
        if len(hits) == 1:
            service = self._services[hits.pop()]
            return ServiceResolution(query=text, service=service, match="contains")
        return ServiceResolution(query=text, candidates=sorted(hits))

    def resolve_environment(self, text: str | None) -> str | None:
        """'prod' -> 'production'. Returns None when unknown."""
        if not text:
            return None
        query = normalize(text)
        for name, entry in self._environments.items():
            if query == normalize(name) or query in {normalize(a) for a in entry.aliases}:
                return name
        return None

    @property
    def environments(self) -> list[str]:
        return list(self._environments)
