"""The integration overlay of one API process (PR-046): load, validate, store, apply.

See ``api/integrations.py`` (the routes) and ``core/integrations.py`` (the overlay rules).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

from pydantic import SecretStr

from aiops.core.config import Settings
from aiops.core.integrations import (
    FieldChange,
    IntegrationError,
    IntegrationOverride,
    IntegrationUpdate,
    apply_override,
    effective_settings,
    plan_update,
    validate_capability,
)
from aiops.core.secrets import SecretBox, SecretsKeyError
from aiops.mcp.client import ServerTarget
from aiops.store.integrations import AuditEntry, IntegrationStore

log = logging.getLogger("aiops.api.integrations")


class IntegrationService:
    """The overlay of one API process: loads it, validates and stores changes, and hands
    back the effective settings. One change at a time (a lock)."""

    def __init__(
        self,
        base: Settings,
        store: IntegrationStore,
        *,
        box: SecretBox | None = None,
        box_error: str | None = None,
        mcp_overrides: Mapping[str, ServerTarget] | None = None,
    ) -> None:
        self.base = base
        self.store = store
        self.box = box
        self.box_error = box_error
        #: capability -> in-process MCP server (tests); used by "Test connection".
        self.mcp_overrides = dict(mcp_overrides or {})
        self.overrides: dict[str, IntegrationOverride] = {}
        self.notes: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls, base: Settings, store: IntegrationStore) -> IntegrationService:
        """The key comes from ``AIOPS_SECRETS_KEY``; an invalid key disables secrets (with
        the reason) instead of stopping the API."""
        try:
            return cls(base, store, box=SecretBox.from_env())
        except SecretsKeyError as exc:
            log.error("integration secrets disabled: %s", exc)
            return cls(base, store, box_error=str(exc))

    @property
    def profile(self) -> str:
        return self.base.profile

    def effective(self) -> Settings:
        settings, self.notes = effective_settings(self.base, self.overrides, self.box)
        for cap, notes in self.notes.items():
            for note in notes:
                log.warning("integration %s: %s", cap, note)
        return settings

    async def load(self) -> Settings:
        self.overrides = await self.store.overrides(self.profile)
        return self.effective()

    def _update(self, update: IntegrationUpdate) -> IntegrationUpdate:
        if self.box is None and self.box_error and any(update.secrets.values()):
            raise IntegrationError(self.box_error, code="secrets_key_invalid")
        return update

    async def save(
        self, capability: str, update: IntegrationUpdate, actor: str
    ) -> tuple[Settings, list[FieldChange], list[str]]:
        """Validate, store (+ audit) and return ``(effective settings, changes, warnings)``.
        Nothing is stored when validation fails or nothing changed."""
        async with self._lock:
            new, changes = plan_update(
                self.base,
                self.overrides.get(capability),
                capability,
                self._update(update),
                self.box,
            )
            apply_override(self.base.capabilities[capability], new, self.box)  # raises if invalid
            candidate = {**self.overrides, capability: new}
            settings, _ = effective_settings(self.base, candidate, self.box)
            warnings = validate_capability(settings, capability)
            if not changes:
                return self.effective(), [], warnings
            await self.store.save(new, actor, "reset" if update.reset else "update", changes)
            if new.empty:
                self.overrides.pop(capability, None)
            else:
                self.overrides[capability] = new
            log.info(
                "integration %s changed by %s: %s",  # field names only, never values
                capability,
                actor,
                ", ".join(f"{c.field} ({c.change})" for c in changes),
            )
            return self.effective(), changes, warnings

    def draft(self, capability: str, update: IntegrationUpdate) -> Settings:
        """The settings with an unsaved change applied (for "Test connection"). Draft
        secrets go straight into the MCP headers: they're never encrypted or stored."""
        plain = IntegrationUpdate(
            enabled=update.enabled,
            provider=update.provider,
            fields=update.fields,
            reset=update.reset,
        )
        new, _ = plan_update(self.base, self.overrides.get(capability), capability, plain, self.box)
        settings, _ = effective_settings(self.base, {**self.overrides, capability: new}, self.box)
        cap = settings.capabilities[capability]
        headers = dict(cap.mcp.headers)
        for name, value in update.secrets.items():
            if value:
                headers[name] = SecretStr(value)
            else:
                headers.pop(name, None)
        if headers != cap.mcp.headers:
            mcp = cap.mcp.model_validate({**cap.mcp.model_dump(), "headers": headers})
            cap = cap.model_copy(update={"mcp": mcp})
            settings = settings.model_copy(
                update={"capabilities": {**settings.capabilities, capability: cap}}
            )
        return settings

    async def audit(self, capability: str | None, limit: int) -> list[AuditEntry]:
        return await self.store.audit(self.profile, capability, limit)
