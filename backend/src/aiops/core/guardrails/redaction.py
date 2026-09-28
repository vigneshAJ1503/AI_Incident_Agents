"""Redact secrets and PII from tool output before it reaches an LLM (MASTER_PLAN §16.3).

JSON payloads are redacted value-by-value (strings only), so numbers such as
timestamps are never corrupted and the JSON stays valid. In JSON, the value of a
secret-looking key (``password``, ``client_secret``, ``Authorization``...) is redacted as a
whole, whatever it looks like.

Kinds (``guardrails.redact`` in the profile; order below = application order):

==================  ===============================================================
private_keys        PEM private keys (RSA/EC/OPENSSH/PKCS#8, GCP service accounts)
connection_strings  passwords in ``scheme://user:pass@host`` URLs and ``Password=`` /
                    ``AccountKey=`` / ``SharedAccessKey=`` / ``sig=`` pairs
cloud_credentials   AWS access key ids + secret keys, GCP API keys and OAuth tokens
saas_tokens         GitHub, GitLab, Slack (incl. webhooks), Atlassian, npm, Stripe
jwt                 JSON Web Tokens
bearer_tokens       ``Bearer <token>``
api_keys            generic ``sk-``-style keys + ``key=value`` / ``"key": "value"`` pairs
                    whose key names a secret; also secret-named JSON keys
emails              e-mail addresses (PII; configurable)
credit_cards        13-19 digit numbers that pass the Luhn check
ip_addresses        IPv4 addresses (PII in some companies; configurable)
==================  ===============================================================

Everything is regex-based and linear: no pattern backtracks catastrophically on long
payloads (tests/security/test_redaction_coverage.py feeds megabytes).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any

Replacer = Callable[[re.Match[str]], str]


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _tag(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def _keep(group: int, kind: str) -> Replacer:
    """Keep the match up to the end of ``group`` (the key), redact the rest (the value)."""
    return lambda m: m.group(0)[: m.end(group) - m.start(0)] + _tag(kind)


#: Key names whose value is a secret (``db_password``, ``client-secret``, ``X-Api-Key``...).
_KEY_NAMES_RE = (
    r"[A-Za-z0-9_.-]{0,40}?(?:api[_-]?key|apikey|secret|password|passwd|pwd|token|access[_-]?key"
    r"|private[_-]?key|credentials?)[A-Za-z0-9_]{0,40}"
)
_SECRET_KEY_NAME = re.compile(
    r"(?i)(?:^|[_.-])(?:api[_-]?key|apikey|secret|password|passwd|pwd|token|access[_-]?key"
    r"|private[_-]?key|client[_-]?secret|authorization|cookie|set-cookie|credentials?)(?:$|[_.-])"
)
_ALREADY = r"(?!\[REDACTED)"

# kind -> list of (pattern, replacer or None for full replacement). Order matters.
_PATTERNS: dict[str, list[tuple[re.Pattern[str], Replacer | None]]] = {
    "private_keys": [
        (
            re.compile(
                r"-----BEGIN[A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----"
                r"(?:[A-Za-z0-9+/=:,\-\s]|\\[nr])*?"
                r"(?:-----END[A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----|$)"
            ),
            None,
        )
    ],
    "connection_strings": [
        # scheme://user:password@host -> scheme://user:[REDACTED]@host
        (
            re.compile(
                r"\b([a-zA-Z][a-zA-Z0-9+.-]{1,30}://[^:/?#@\s\"']{0,256}:)([^@\s\"'/]{1,512})@"
            ),
            lambda m: f"{m.group(1)}{_tag('connection_strings')}@",
        ),
        # Azure: AccountKey=...; SharedAccessKey=...; sig=... (Password= is api_keys)
        (
            re.compile(
                r"(?i)\b(accountkey|sharedaccesskey|sharedaccesssignature|sig)"
                r"=" + _ALREADY + r"[^;&\s\"']{3,}"
            ),
            lambda m: f"{m.group(1)}={_tag('connection_strings')}",
        ),
    ],
    "cloud_credentials": [
        (re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA|AROA|AIDA)[0-9A-Z]{16}\b"), None),
        (
            re.compile(
                r"(?i)(aws_?secret(?:_?access)?_?key[\"']?\s*[:=]\s*[\"']?)"
                r"[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])"
            ),
            _keep(1, "cloud_credentials"),
        ),
        (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), None),  # GCP API key
        (re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"), None),  # GCP OAuth access token
        (re.compile(r"\b1//0[0-9A-Za-z_-]{30,}"), None),  # GCP refresh token
    ],
    "saas_tokens": [
        (
            re.compile(
                r"\bgh[pousr]_[A-Za-z0-9]{30,}"  # GitHub classic
                r"|\bgithub_pat_[A-Za-z0-9_]{40,}"  # GitHub fine-grained
                r"|\bgl(?:pat|dt|rt|cbt|ptt|oas|ft|imt|agent|soat)-[A-Za-z0-9_-]{20,}"  # GitLab
                r"|\bxox[abposre]-[A-Za-z0-9-]{10,}"  # Slack
                r"|\bxapp-[0-9]-[A-Za-z0-9-]{10,}"  # Slack app-level
                r"|https://hooks\.slack\.com/(?:services|workflows)/[A-Za-z0-9/_-]{20,}"
                r"|\bATATT3[A-Za-z0-9_=-]{20,}"  # Atlassian API token
                r"|\bATCTT3[A-Za-z0-9_=-]{20,}"  # Atlassian Connect
                r"|\bnpm_[A-Za-z0-9]{36}"  # npm
                r"|\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"  # Stripe
            ),
            None,
        )
    ],
    "jwt": [(re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), None)],
    "bearer_tokens": [
        (
            re.compile(r"(?i)\b(bearer)\s+" + _ALREADY + r"[A-Za-z0-9._~+/=-]{8,}"),
            lambda m: f"{m.group(1)} {_tag('bearer_tokens')}",
        ),
        (
            re.compile(
                r"(?i)\b(authorization[\"']?\s*[:=]\s*[\"']?basic)\s+[A-Za-z0-9+/]{8,}={0,2}"
            ),
            lambda m: f"{m.group(1)} {_tag('bearer_tokens')}",
        ),
    ],
    "api_keys": [
        (
            re.compile(
                r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"
                r"|\bgsk_[A-Za-z0-9]{20,}"
                r"|\bAKIA[0-9A-Z]{16}\b"
                r"|\bgh[pousr]_[A-Za-z0-9]{30,}"
                r"|\bxox[abpr]-[A-Za-z0-9-]{10,}"
                r"|\bAIza[0-9A-Za-z_-]{35}"
            ),
            None,
        ),
        # key=value, key: value, "key": "value" (also inside JSON-in-a-log-line)
        (
            re.compile(
                r"(?i)(?<![A-Za-z0-9])("
                + _KEY_NAMES_RE
                + r")(\\?[\"']?\s*[:=]\s*\\?[\"']?)"
                + _ALREADY
                + r"(?!\d{1,9}\b)[^\s'\",;&}\\]{4,}"
            ),
            lambda m: f"{m.group(1)}{m.group(2)}{_tag('api_keys')}",
        ),
    ],
    "emails": [(re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), None)],
    "credit_cards": [
        (
            re.compile(r"\b\d(?:[ -]?\d){12,18}\b"),
            lambda m: (
                _tag("credit_cards") if _luhn_ok(re.sub(r"\D", "", m.group(0))) else m.group(0)
            ),
        )
    ],
    "ip_addresses": [
        (re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"), None)
    ],
}

SUPPORTED_KINDS = frozenset(_PATTERNS)
#: Every secret kind (no PII): a safe default for any profile.
SECRET_KINDS = (
    "private_keys",
    "connection_strings",
    "cloud_credentials",
    "saas_tokens",
    "jwt",
    "bearer_tokens",
    "api_keys",
)
#: PII kinds a company may or may not want redacted (emails can be needed to correlate).
PII_KINDS = ("emails", "credit_cards", "ip_addresses")


def is_secret_key(name: str) -> bool:
    """Whether a JSON/header key names a secret (``db_password``, ``X-Api-Key``...)."""
    return bool(_SECRET_KEY_NAME.search(name))


class Redactor:
    def __init__(self, kinds: Iterable[str]) -> None:
        kinds = list(kinds)
        unknown = set(kinds) - SUPPORTED_KINDS
        if unknown:
            raise ValueError(
                f"Unknown redaction kinds {sorted(unknown)}; supported: {sorted(SUPPORTED_KINDS)}"
            )
        # Keep canonical order so e.g. JWTs are removed before generic token patterns.
        self._kinds = [k for k in _PATTERNS if k in kinds]
        self._secret_keys = "api_keys" in self._kinds
        self.counts: Counter[str] = Counter()

    def text(self, value: str) -> str:
        for kind in self._kinds:
            for pattern, replacer in _PATTERNS[kind]:

                def _replace(
                    match: re.Match[str], kind: str = kind, replacer: Replacer | None = replacer
                ) -> str:
                    out = replacer(match) if replacer else _tag(kind)
                    if out != match.group(0):
                        self.counts[kind] += 1
                    return out

                value = pattern.sub(_replace, value)
        return value

    def data(self, value: Any) -> Any:
        """Recursively redact string values (and keys) of JSON-like data. A string value
        under a secret-named key is replaced as a whole (``api_keys``)."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, item in value.items():
                name = str(key)
                if (
                    self._secret_keys
                    and isinstance(item, str)
                    and item
                    and not item.startswith("[REDACTED")
                    and is_secret_key(name)
                ):
                    self.counts["api_keys"] += 1
                    out[self.text(name)] = _tag("api_keys")
                else:
                    out[self.text(name)] = self.data(item)
            return out
        if isinstance(value, list):
            return [self.data(v) for v in value]
        return value
