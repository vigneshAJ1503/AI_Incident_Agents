"""Encryption at rest for secrets saved from the Web UI (PR-046, docs/portability.md).

Secrets typed into Settings -> Integrations (an MCP server's ``Authorization`` header, ...)
are stored in Postgres encrypted with Fernet (AES-128-CBC + HMAC-SHA256, from
``cryptography``). The key comes from ``AIOPS_SECRETS_KEY`` and never from the database or a
profile. Without it, the API refuses to store secrets (with a message saying how to set it)
instead of storing them in clear.

Generate a key once and keep it in the deployment's secret manager::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Losing or rotating the key makes the stored secrets unreadable: the API then reports them as
``usable: false`` and they have to be typed again. Nothing here ever logs a value.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

SECRETS_KEY_VAR = "AIOPS_SECRETS_KEY"
#: Shown values keep at most this many trailing characters, and only of long values.
HINT_CHARS = 4
HINT_MIN_LENGTH = 12

KEY_HELP = (
    f"set {SECRETS_KEY_VAR} on the API to a Fernet key (generate one with: python -c "
    '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
    ") and restart it, or keep the secret in the profile's .env as a ${VAR} reference"
)


class SecretsKeyError(Exception):
    """No usable encryption key: secrets can't be stored or read. Message is for humans."""


class SecretBox:
    """Encrypts and decrypts short secret strings with the deployment's key."""

    def __init__(self, key: str | bytes) -> None:
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise SecretsKeyError(
                f"{SECRETS_KEY_VAR} is not a valid Fernet key (32 url-safe base64 bytes): "
                + KEY_HELP
            ) from exc

    @classmethod
    def from_env(cls) -> SecretBox | None:
        """The box of ``$AIOPS_SECRETS_KEY``; ``None`` when it isn't set. An invalid key
        raises :class:`SecretsKeyError`."""
        key = os.environ.get(SECRETS_KEY_VAR, "").strip()
        return cls(key) if key else None

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise SecretsKeyError(
                f"a stored secret can't be decrypted with this {SECRETS_KEY_VAR} (was the key "
                "rotated?): save the secret again"
            ) from exc


def secret_hint(value: str) -> str | None:
    """The last ``HINT_CHARS`` characters of a long secret, else nothing."""
    return value[-HINT_CHARS:] if len(value) >= HINT_MIN_LENGTH else None
