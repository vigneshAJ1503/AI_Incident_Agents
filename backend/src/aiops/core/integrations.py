"""Integration overrides from the Web UI, layered over the active profile (PR-046).

The YAML profile (``profiles/<name>/profile.yaml``) stays the source of defaults. Settings
-> Integrations saves only what a person changed, per capability, as an *override*:

* ``enabled`` and ``provider`` (an implemented provider of the capability);
* ``fields``: dotted keys of the capability config: ``mcp.url``, ``mcp.timeout_s``,
  ``limits.{max_results,query_timeout_s,max_time_range_hours}`` and any ``settings.*`` key
  the profile has (field names, labels, link templates, ...), typed like the profile value;
* ``secrets``: HTTP headers for an MCP server behind auth (``mcp.headers``), write-only and
  encrypted at rest (``core/secrets.py``).

Tool allowlists are deliberately *not* editable here: they are the agents' read-only
security boundary and stay in reviewed YAML.

``effective_settings`` = profile + overrides. A change is validated with the same Pydantic
models as the YAML (``CapabilityConfig``) and ``aiops profile validate``'s semantic checks
before it is stored; the API then swaps in the new settings for *new* investigations
(running ones keep theirs).
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import SecretStr, ValidationError

from aiops.core.config import (
    HEADER_NAME,
    PROFILE_FILE,
    CapabilityConfig,
    ConfigError,
    Settings,
    load_extending_yaml,
)
from aiops.core.profiles import PROVIDERS, check_settings
from aiops.core.secrets import KEY_HELP, SecretBox, SecretsKeyError, secret_hint

#: The capabilities the Web UI manages (every capability an agent binds to).
CAPABILITIES: tuple[str, ...] = tuple(PROVIDERS)
EDITABLE_LIMITS = ("max_results", "query_timeout_s", "max_time_range_hours")
MAX_STRING = 2_048
MAX_LIST = 100
MAX_SECRET = 4_096

FieldType = Literal["string", "number", "boolean", "list", "json"]
Change = Literal["set", "updated", "reverted", "cleared"]

#: Setting keys that hold credentials. They are never shown or edited as plain fields
#: (``project_key`` is not one: the name part must be a whole word).
_SECRET_NAME = re.compile(
    r"(^|[_.-])(api_?key|apikey|token|secret|password|passwd|credentials?)([_.-]|$)",
    re.IGNORECASE,
)
#: Guardrail settings (approvals, allowlists, read-only switches) stay in reviewed YAML.
_LOCKED_NAME = re.compile(r"(approval|allowlist|read_only|readonly)", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*)*$")


class IntegrationError(Exception):
    """A change that can't be saved; the message is for humans (HTTP 422 by default)."""

    def __init__(self, message: str, code: str = "invalid_integration", status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


# --------------------------------------------------------------------------- model


@dataclass(frozen=True)
class StoredSecret:
    ciphertext: str
    hint: str | None = None  # the last 4 characters of a long value, else None


@dataclass
class IntegrationOverride:
    """What the UI changed for one capability of one profile."""

    profile: str
    capability: str
    enabled: bool | None = None
    provider: str | None = None
    #: Dotted keys (``mcp.url``, ``limits.max_results``, ``settings.fields.level``) -> value.
    fields: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, StoredSecret] = field(default_factory=dict)
    updated_at: datetime | None = None
    updated_by: str | None = None

    @property
    def empty(self) -> bool:
        return (
            self.enabled is None and self.provider is None and not self.fields and not self.secrets
        )

    def document(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"fields": dict(self.fields)}
        if self.enabled is not None:
            doc["enabled"] = self.enabled
        if self.provider is not None:
            doc["provider"] = self.provider
        return doc

    def overridden(self) -> list[str]:
        keys = [k for k in ("enabled", "provider") if getattr(self, k) is not None]
        return [*keys, *sorted(self.fields), *(f"secrets.{n}" for n in sorted(self.secrets))]


@dataclass(frozen=True)
class FieldChange:
    field: str  # e.g. "mcp.url", "settings.fields.level", "secrets.Authorization"
    change: Change


@dataclass(frozen=True)
class IntegrationUpdate:
    """A requested change. ``None`` in ``fields``/``secrets`` = back to the profile value."""

    enabled: bool | None = None
    provider: str | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)
    secrets: Mapping[str, str | None] = field(default_factory=dict)
    #: Drop every override of the capability first ("reset to profile").
    reset: bool = False


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_NAME.search(key))


def is_locked_key(key: str) -> bool:
    """A guardrail setting: shown, never editable from the UI."""
    return bool(_LOCKED_NAME.search(key))


# --------------------------------------------------------------------------- dotted keys


def _flatten(value: Any, prefix: str) -> dict[str, Any]:
    if isinstance(value, dict) and value:
        out: dict[str, Any] = {}
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}.{k}"))
        return out
    return {prefix: value}


def _get(data: Mapping[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _set(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    current = data
    for part in parts[:-1]:
        nxt = current.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            current[part] = nxt
        current = nxt
    current[parts[-1]] = value


def field_type(value: Any) -> FieldType:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if value is None or isinstance(value, str):
        return "string"
    if isinstance(value, list) and all(
        isinstance(v, str | int | float) and not isinstance(v, bool) for v in value
    ):
        return "list"
    return "json"


def _raw_settings(settings: Settings, capability: str) -> dict[str, Any]:
    """``settings.*`` keys the profile YAML declares (also those whose ``${VAR:-}`` resolved
    empty, e.g. an unset ``ui_link_template``), so the UI can fill them in."""
    if not settings.profile_chain:
        return {}
    try:
        raw = load_extending_yaml(settings.profile_chain[0] / PROFILE_FILE)
    except ConfigError:
        return {}
    cap = (raw.get("capabilities") or {}).get(capability) or {}
    raw_settings = cap.get("settings") if isinstance(cap, dict) else None
    return _flatten(raw_settings, "settings") if isinstance(raw_settings, dict) else {}


def editable_defaults(base: Settings, capability: str) -> dict[str, Any]:
    """Every field key the UI may set for ``capability`` -> the profile's value (``None``
    when the profile leaves it empty)."""
    cap = base.capabilities.get(capability)
    if cap is None:
        return {}
    defaults: dict[str, Any] = {}
    if cap.mcp.transport == "http":
        defaults["mcp.url"] = cap.mcp.url
    defaults["mcp.timeout_s"] = cap.mcp.timeout_s
    for name in EDITABLE_LIMITS:
        defaults[f"limits.{name}"] = getattr(cap.limits, name)
    declared = _raw_settings(base, capability)
    for key in declared:
        defaults.setdefault(key, None)
    for key, value in (_flatten(cap.settings, "settings") if cap.settings else {}).items():
        defaults[key] = value
    return defaults


# --------------------------------------------------------------------------- apply


def mask_url(url: str | None) -> str | None:
    """A URL without its ``user:password@`` part."""
    if not url:
        return url
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, "***@" + netloc, parts.path, parts.query, parts.fragment))


def apply_override(
    cap: CapabilityConfig, override: IntegrationOverride, box: SecretBox | None
) -> tuple[CapabilityConfig, list[str]]:
    """``cap`` with ``override`` applied, validated by ``CapabilityConfig``. Secrets that
    can't be decrypted (no/wrong key) are left out, with a note. Raises IntegrationError."""
    data = cap.model_dump()
    notes: list[str] = []
    if override.enabled is not None:
        data["enabled"] = override.enabled
    if override.provider is not None:
        data["provider"] = override.provider
    for key, value in override.fields.items():
        _set(data, key, copy.deepcopy(value))
    headers = dict(data["mcp"].get("headers") or {})
    for name, stored in override.secrets.items():
        if box is None:
            notes.append(f"secret {name} is stored but {KEY_HELP.split(' (')[0]} to use it")
            continue
        try:
            headers[name] = SecretStr(box.decrypt(stored.ciphertext))
        except SecretsKeyError as exc:
            notes.append(f"secret {name}: {exc}")
    data["mcp"]["headers"] = headers
    try:
        return CapabilityConfig.model_validate(data), notes
    except ValidationError as err:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']}" for e in err.errors()
        )
        raise IntegrationError(f"capability {override.capability}: {problems}") from err


def effective_settings(
    base: Settings, overrides: Mapping[str, IntegrationOverride], box: SecretBox | None
) -> tuple[Settings, dict[str, list[str]]]:
    """The profile with every override applied. An override that no longer validates (e.g.
    the YAML changed underneath it) is skipped with a note instead of breaking the API."""
    caps = dict(base.capabilities)
    notes: dict[str, list[str]] = {}
    for name, override in overrides.items():
        cap = caps.get(name)
        if cap is None:
            notes.setdefault(name, []).append(
                f"override ignored: capability {name} is not in profile '{base.profile}'"
            )
            continue
        try:
            caps[name], cap_notes = apply_override(cap, override, box)
        except IntegrationError as exc:
            notes.setdefault(name, []).append(f"override ignored: {exc}")
            continue
        if cap_notes:
            notes.setdefault(name, []).extend(cap_notes)
    return base.model_copy(update={"capabilities": caps}), notes


# --------------------------------------------------------------------------- update


def _coerce(key: str, default: Any, value: Any) -> Any:
    """``value`` checked against the type of the profile's ``default``."""
    kind = field_type(default)
    where = f"field {key}"
    if kind == "json":
        raise IntegrationError(f"{where} is structured; edit it in profile.yaml")
    if default is None:  # declared but empty in the profile: a string (or a list of them)
        if isinstance(value, list):
            kind = "list"
        elif isinstance(value, str):
            kind = "string"
        else:
            raise IntegrationError(f"{where} must be text")
    if kind == "boolean":
        if not isinstance(value, bool):
            raise IntegrationError(f"{where} must be true or false")
        return value
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise IntegrationError(f"{where} must be a number")
        if isinstance(default, int) and isinstance(value, float):
            if not value.is_integer():
                raise IntegrationError(f"{where} must be a whole number")
            return int(value)
        return value
    if kind == "string":
        if not isinstance(value, str):
            raise IntegrationError(f"{where} must be text")
        if len(value) > MAX_STRING or _CONTROL.search(value):
            raise IntegrationError(
                f"{where}: at most {MAX_STRING} characters, no control characters"
            )
        return value
    # list
    if not isinstance(value, list) or len(value) > MAX_LIST:
        raise IntegrationError(f"{where} must be a list (at most {MAX_LIST} items)")
    numeric = bool(default) and all(isinstance(v, int | float) for v in default)
    out: list[Any] = []
    for item in value:
        if numeric:
            if isinstance(item, bool) or not isinstance(item, int | float):
                raise IntegrationError(f"{where}: every item must be a number")
        elif not isinstance(item, str) or len(item) > 256 or _CONTROL.search(item):
            raise IntegrationError(f"{where}: every item must be a short text")
        out.append(item)
    return out


def _check_url(value: Any) -> str:
    if not isinstance(value, str) or not re.match(r"^https?://[^\s/]+", value):
        raise IntegrationError(
            "field mcp.url must be an http(s) URL, e.g. http://logs-mcp:8101/mcp"
        )
    parts = urlsplit(value)
    if parts.username is not None or parts.password is not None:
        raise IntegrationError(
            "field mcp.url must not contain credentials (user:password@): save them as a "
            "secret header (e.g. Authorization) instead"
        )
    return value


def plan_update(
    base: Settings,
    current: IntegrationOverride | None,
    capability: str,
    update: IntegrationUpdate,
    box: SecretBox | None,
) -> tuple[IntegrationOverride, list[FieldChange]]:
    """The override after ``update`` + what changed (field names only). Types, keys,
    providers and secrets are checked here; ``validate_capability`` checks the result."""
    cap = base.capabilities.get(capability)
    if cap is None:
        raise IntegrationError(
            f"capability {capability} is not configured in profile '{base.profile}': add it "
            "to profile.yaml first (the Web UI edits configured capabilities)",
            code="not_configured",
            status=409,
        )
    before = current or IntegrationOverride(base.profile, capability)
    new = replace(
        IntegrationOverride(base.profile, capability) if update.reset else before,
        fields={} if update.reset else dict(before.fields),
        secrets={} if update.reset else dict(before.secrets),
        updated_at=before.updated_at,
        updated_by=before.updated_by,
    )
    changes: list[FieldChange] = []
    if update.reset:
        changes.extend(FieldChange(k, "reverted") for k in before.overridden())

    def record(key: str, old: Any, value: Any, default: Any) -> None:
        if value == old:
            return
        if value is None or value == default:
            changes.append(FieldChange(key, "reverted"))
        else:
            changes.append(FieldChange(key, "updated" if old is not None else "set"))

    if update.enabled is not None:
        old = new.enabled
        new.enabled = None if update.enabled == cap.enabled else update.enabled
        record("enabled", old, new.enabled, None)
    if update.provider is not None:
        implemented = sorted(
            p for p, s in PROVIDERS.get(capability, {}).items() if s.status == "implemented"
        )
        if update.provider not in implemented:
            raise IntegrationError(
                f"provider '{update.provider}' is not an implemented {capability} provider "
                f"(implemented: {', '.join(implemented) or 'none'})"
            )
        old_provider = new.provider
        new.provider = None if update.provider == cap.provider else update.provider
        record("provider", old_provider, new.provider, None)

    defaults = editable_defaults(base, capability)
    for key, value in update.fields.items():
        if not _DOTTED.match(key) or key not in defaults:
            raise IntegrationError(
                f"unknown field {key!r} for {capability} (editable: mcp.url, mcp.timeout_s, "
                "limits.*, and the settings.* keys of the profile)"
            )
        if is_secret_key(key):
            raise IntegrationError(f"field {key} holds a credential: save it as a secret")
        if is_locked_key(key):
            raise IntegrationError(
                f"field {key} is a guardrail setting: change it in the reviewed profile.yaml"
            )
        old = new.fields.get(key)
        if value is None or value == defaults[key]:
            new.fields.pop(key, None)
            if old is not None:
                changes.append(FieldChange(key, "reverted"))
            continue
        checked = _check_url(value) if key == "mcp.url" else _coerce(key, defaults[key], value)
        new.fields[key] = checked
        record(key, old, checked, defaults[key])

    for name, value in update.secrets.items():
        if not HEADER_NAME.match(name):
            raise IntegrationError(
                f"secret name {name!r} must be an HTTP header name (letters, digits, '-'), "
                "e.g. Authorization"
            )
        key = f"secrets.{name}"
        if value is None or value == "":
            if new.secrets.pop(name, None) is not None:
                changes.append(FieldChange(key, "cleared"))
            continue
        if cap.mcp.transport != "http":
            raise IntegrationError(
                f"secrets are sent as HTTP headers to the MCP server; {capability} uses "
                f"transport '{cap.mcp.transport}'"
            )
        if len(value) > MAX_SECRET or _CONTROL.search(value):
            raise IntegrationError(
                f"secret {name}: a single line of at most {MAX_SECRET} characters"
            )
        if box is None:
            raise IntegrationError(
                f"secret {name} can't be stored: no encryption key. To save secrets, " + KEY_HELP,
                code="secrets_key_missing",
            )
        existed = name in new.secrets
        new.secrets[name] = StoredSecret(box.encrypt(value), secret_hint(value))
        changes.append(FieldChange(key, "updated" if existed else "set"))
    return new, changes


def validate_capability(settings: Settings, capability: str) -> list[str]:
    """``aiops profile validate``'s checks for one capability of ``settings``: raises
    IntegrationError on errors, returns the warnings."""
    report = check_settings(settings)
    prefix = f"capability {capability}: "
    errors = [i.message.removeprefix(prefix) for i in report.errors if i.message.startswith(prefix)]
    if errors:
        raise IntegrationError("; ".join(errors))
    return [i.message.removeprefix(prefix) for i in report.warnings if i.message.startswith(prefix)]


# --------------------------------------------------------------------------- describe


def _public(key: str, value: Any) -> Any:
    return mask_url(value) if key == "mcp.url" else value


def describe(
    capability: str,
    base: Settings,
    effective: Settings,
    override: IntegrationOverride | None,
    status: str,
    box: SecretBox | None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """The ``Integration`` resource of docs/api/contract.md. Secret values never appear:
    only whether a secret is configured and, for long ones, its last 4 characters."""
    base_cap = base.capabilities.get(capability)
    cap = effective.capabilities.get(capability) or base_cap
    implemented = sorted(
        p for p, s in PROVIDERS.get(capability, {}).items() if s.status == "implemented"
    )
    if cap is None or base_cap is None:
        return {
            "capability": capability,
            "configured": False,
            "provider": None,
            "providers": implemented,
            "enabled": False,
            "status": "not_configured",
            "transport": None,
            "fields": [],
            "tool_allowlist": [],
            "secrets": [],
            "overridden": [],
            "updated_at": None,
            "updated_by": None,
            "notes": list(notes or []),
        }
    defaults = editable_defaults(base, capability)
    current = {
        "mcp.url": cap.mcp.url,
        "mcp.timeout_s": cap.mcp.timeout_s,
        **{f"limits.{n}": getattr(cap.limits, n) for n in EDITABLE_LIMITS},
        **(_flatten(cap.settings, "settings") if cap.settings else {}),
    }
    fields = []
    for key, default in defaults.items():
        value = current.get(key)
        secret = is_secret_key(key)
        kind = field_type(default if default is not None else value)
        fields.append(
            {
                "key": key,
                "value": None if secret else _public(key, value),
                "default": None if secret else _public(key, default),
                "type": kind,
                "overridden": bool(override and key in override.fields),
                "editable": not secret and kind != "json" and not is_locked_key(key),
                "secret": secret,
            }
        )
    secrets: dict[str, dict[str, Any]] = {
        name: {
            "name": name,
            "configured": bool(value.get_secret_value()),
            "last4": None,
            "source": "profile",
            "usable": True,
        }
        for name, value in base_cap.mcp.headers.items()
    }
    for name, stored in (override.secrets if override else {}).items():
        usable = False
        if box is not None:
            try:
                box.decrypt(stored.ciphertext)
                usable = True
            except SecretsKeyError:
                usable = False
        secrets[name] = {
            "name": name,
            "configured": True,
            "last4": stored.hint,
            "source": "ui",
            "usable": usable,
        }
    return {
        "capability": capability,
        "configured": True,
        "provider": cap.provider,
        "providers": sorted({*implemented, base_cap.provider}),
        "enabled": cap.enabled,
        "status": status,
        "transport": cap.mcp.transport,
        "fields": fields,
        "tool_allowlist": list(cap.tool_allowlist),
        "secrets": [secrets[n] for n in sorted(secrets)],
        "overridden": override.overridden() if override else [],
        "updated_at": override.updated_at if override else None,
        "updated_by": override.updated_by if override else None,
        "notes": list(notes or []),
    }
