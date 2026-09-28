"""Provider registry: ``(capability, provider)`` -> adapter class.

Provider modules register themselves on import (``aiops.providers`` discovers them, like
agents); ``core.profiles`` reads the registry to know which providers are implemented.
"""

from __future__ import annotations

from aiops.core.config import ConfigError, Settings
from aiops.providers.base import Provider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[tuple[str, str], type[Provider]] = {}

    def register[C: type[Provider]](self, provider_cls: C) -> C:
        key = provider_cls.key()
        existing = self._providers.get(key)
        if existing is not None and existing is not provider_cls:
            raise ValueError(f"Provider {key[0]}/{key[1]} is already registered")
        self._providers[key] = provider_cls
        return provider_cls

    def find(self, capability: str, name: str) -> type[Provider] | None:
        return self._providers.get((capability, name))

    def get(self, capability: str, name: str) -> type[Provider]:
        found = self.find(capability, name)
        if found is None:
            known = ", ".join(self.names(capability)) or "none"
            raise ConfigError(
                f"capability {capability}: no provider adapter '{name}' (implemented: {known})"
            )
        return found

    def names(self, capability: str) -> list[str]:
        return sorted(n for c, n in self._providers if c == capability)

    def capabilities(self) -> list[str]:
        return sorted({c for c, _ in self._providers})

    def all(self) -> list[type[Provider]]:
        return [cls for _, cls in sorted(self._providers.items())]

    def for_settings[P: Provider](self, settings: Settings, capability: str, base: type[P]) -> P:
        """The adapter the profile selects for ``capability``, built with its settings."""
        cap = settings.capability(capability)
        cls = self.get(capability, cap.provider)
        if not issubclass(cls, base):
            raise ConfigError(
                f"capability {capability}: provider '{cap.provider}' is not a {base.__name__}"
            )
        return cls(cap.settings)


#: Default registry; provider modules register on import (see aiops.providers).
PROVIDER_REGISTRY = ProviderRegistry()
