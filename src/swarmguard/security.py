"""Central security configuration and primitives for SwarmGuard.

This module is the single source of truth for environment-driven, fail-closed
security behaviour. In ``development`` (the default) explicit local-demo
behaviour is preserved so the demo works without secret provisioning. In
``production`` the helpers fail closed for missing/default/weak secrets,
permissive network exposure, and unencrypted retention of sensitive data.

It intentionally depends only on the standard library plus ``cryptography``
(for AES-256-GCM). Bearer tokens are HMAC-SHA256 signed and dependency-light;
no third-party JWT/auth dependency is required.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SecurityConfigError(RuntimeError):
    """Raised when a fail-closed security invariant is violated at config time."""


class AuthError(Exception):
    """Raised when a bearer token or credential fails authentication."""


class DecryptionError(Exception):
    """Raised when ciphertext cannot be authenticated/decrypted."""


# ---------------------------------------------------------------------------
# Environment mode
# ---------------------------------------------------------------------------

DEVELOPMENT = "development"
PRODUCTION = "production"
_VALID_ENVIRONMENTS = {DEVELOPMENT, PRODUCTION}

Getenv = Callable[[str], "str | None"]


def _getenv(getenv: Getenv | None) -> Getenv:
    return getenv if getenv is not None else os.environ.get


def get_environment(getenv: Getenv | None = None) -> str:
    value = (_getenv(getenv)("SWARMGUARD_ENV") or DEVELOPMENT).strip().lower()
    if value not in _VALID_ENVIRONMENTS:
        raise SecurityConfigError(
            f"SWARMGUARD_ENV must be one of {sorted(_VALID_ENVIRONMENTS)}, got: {value!r}"
        )
    return value


def is_production(getenv: Getenv | None = None) -> bool:
    return get_environment(getenv) == PRODUCTION


MIN_SECRET_LENGTH = 32


def _is_strong_secret(value: str | None, *, forbidden_defaults: Iterable[str]) -> bool:
    if not value:
        return False
    if value in set(forbidden_defaults):
        return False
    return len(value) >= MIN_SECRET_LENGTH


# ---------------------------------------------------------------------------
# Roles / RBAC ladder
# ---------------------------------------------------------------------------

VIEWER = "viewer"
OPERATOR = "operator"
ADMIN = "admin"
_ROLE_RANK = {VIEWER: 1, OPERATOR: 2, ADMIN: 3}


def _role_rank(role: str) -> int:
    try:
        return _ROLE_RANK[role]
    except KeyError as exc:
        raise SecurityConfigError(f"unknown role: {role!r}") from exc


def role_satisfies(actual: str, required: str) -> bool:
    """True when ``actual`` is at least as privileged as ``required``."""
    return _role_rank(actual) >= _role_rank(required)


# ---------------------------------------------------------------------------
# Bearer tokens (dependency-light, HMAC-SHA256 signed, short-lived)
# ---------------------------------------------------------------------------

TOKEN_ISSUER = "swarmguard"
TOKEN_AUDIENCE = "swarmguard-api"
DEFAULT_AUTH_SIGNING_KEY = "swarmguard-dev-api-signing-key-not-for-production"
_TOKEN_HEADER = {"alg": "HS256", "typ": "SGT"}


@dataclass(frozen=True)
class TokenClaims:
    sub: str
    role: str
    iss: str
    aud: str
    iat: float
    exp: float


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _encode_token(payload: dict[str, Any], key: bytes) -> str:
    header_segment = _b64url_encode(
        json.dumps(_TOKEN_HEADER, sort_keys=True, separators=(",", ":")).encode()
    )
    payload_segment = _b64url_encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    )
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = hmac.new(key, signing_input, hashlib.sha256).digest()
    return f"{header_segment}.{payload_segment}.{_b64url_encode(signature)}"


def _coerce_key(key: str | bytes | None, getenv: Getenv | None) -> bytes:
    if key is None:
        return resolve_auth_signing_key(getenv)
    return key.encode() if isinstance(key, str) else key


def resolve_auth_signing_key(getenv: Getenv | None = None) -> bytes:
    getenv = _getenv(getenv)
    value = getenv("SWARMGUARD_API_SIGNING_KEY")
    if is_production(getenv):
        if not _is_strong_secret(value, forbidden_defaults=[DEFAULT_AUTH_SIGNING_KEY]):
            raise SecurityConfigError(
                "production requires SWARMGUARD_API_SIGNING_KEY to be a non-default "
                f"secret of at least {MIN_SECRET_LENGTH} characters"
            )
        return value.encode()
    return (value or DEFAULT_AUTH_SIGNING_KEY).encode()


def mint_token(
    subject: str,
    role: str,
    *,
    key: str | bytes | None = None,
    ttl_seconds: int = 900,
    issuer: str = TOKEN_ISSUER,
    audience: str = TOKEN_AUDIENCE,
    now: float | None = None,
    getenv: Getenv | None = None,
) -> str:
    if role not in _ROLE_RANK:
        raise SecurityConfigError(f"cannot mint token with unknown role: {role!r}")
    issued_at = time.time() if now is None else now
    payload = {
        "sub": subject,
        "role": role,
        "iss": issuer,
        "aud": audience,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
    }
    return _encode_token(payload, _coerce_key(key, getenv))


def verify_token(
    token: str,
    *,
    key: str | bytes | None = None,
    issuer: str = TOKEN_ISSUER,
    audience: str = TOKEN_AUDIENCE,
    now: float | None = None,
    getenv: Getenv | None = None,
) -> TokenClaims:
    if not token or token.count(".") != 2:
        raise AuthError("malformed token")
    header_segment, payload_segment, signature_segment = token.split(".")
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    expected = hmac.new(_coerce_key(key, getenv), signing_input, hashlib.sha256).digest()
    try:
        provided = _b64url_decode(signature_segment)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise AuthError("malformed token signature") from exc
    if not hmac.compare_digest(expected, provided):
        raise AuthError("invalid token signature")
    try:
        payload = json.loads(_b64url_decode(payload_segment))
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise AuthError("malformed token payload") from exc
    if not isinstance(payload, dict):
        raise AuthError("malformed token payload")

    if payload.get("iss") != issuer:
        raise AuthError("invalid token issuer")
    if payload.get("aud") != audience:
        raise AuthError("invalid token audience")
    role = payload.get("role")
    if role not in _ROLE_RANK:
        raise AuthError("token carries an unknown role")
    try:
        exp = float(payload["exp"])
        iat = float(payload["iat"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthError("token is missing timestamps") from exc
    current = time.time() if now is None else now
    if current >= exp:
        raise AuthError("token is expired")
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise AuthError("token is missing subject")
    return TokenClaims(sub=subject, role=role, iss=issuer, aud=audience, iat=iat, exp=exp)


# ---------------------------------------------------------------------------
# Harness signing key
# ---------------------------------------------------------------------------

DEFAULT_HARNESS_SIGNING_KEY = "swarmguard-dev-harness"


def resolve_harness_signing_key(getenv: Getenv | None = None) -> str:
    getenv = _getenv(getenv)
    value = getenv("SWARMGUARD_HARNESS_SIGNING_KEY")
    if is_production(getenv):
        if not _is_strong_secret(value, forbidden_defaults=[DEFAULT_HARNESS_SIGNING_KEY]):
            raise SecurityConfigError(
                "production requires SWARMGUARD_HARNESS_SIGNING_KEY to be a non-default "
                f"secret of at least {MIN_SECRET_LENGTH} characters"
            )
        return value
    return value or DEFAULT_HARNESS_SIGNING_KEY


# ---------------------------------------------------------------------------
# Secret classification / redaction
# ---------------------------------------------------------------------------

REDACTION_PLACEHOLDER = "[REDACTED]"

# Substrings that mark a mapping key as sensitive (case-insensitive, ignoring
# non-alphanumeric separators so ``api_key``/``apiKey``/``API-KEY`` all match).
_SENSITIVE_KEY_SUBSTRINGS = (
    "apikey",
    "api_key",
    "authorization",
    "token",
    "password",
    "passwd",
    "secret",
    "credential",
    "privatekey",
    "private_key",
    "accesskey",
    "session",
    "cookie",
    "bearer",
)

# Value patterns that look like secrets even under a benign key.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def is_sensitive_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return any(substring in normalized for substring in _SENSITIVE_KEY_SUBSTRINGS)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        for pattern in _SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                return REDACTION_PLACEHOLDER
        return value
    return redact(value)


def redact(value: Any) -> Any:
    """Recursively redact sensitive keys/values, returning a new structure."""
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and is_sensitive_key(key):
                result[key] = REDACTION_PLACEHOLDER
            else:
                result[key] = _redact_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_value(value)
    return value


# ---------------------------------------------------------------------------
# Encryption of retained sensitive payloads (AES-256-GCM)
# ---------------------------------------------------------------------------

ENCRYPTION_ALG = "AES-256-GCM"
DEFAULT_ENCRYPTION_KEY = "swarmguard-dev-audit-encryption-key-not-for-prod!!"


def resolve_encryption_key(getenv: Getenv | None = None) -> bytes:
    getenv = _getenv(getenv)
    value = getenv("SWARMGUARD_AUDIT_ENCRYPTION_KEY")
    if is_production(getenv):
        if not _is_strong_secret(value, forbidden_defaults=[DEFAULT_ENCRYPTION_KEY]):
            raise SecurityConfigError(
                "production requires SWARMGUARD_AUDIT_ENCRYPTION_KEY to be a non-default "
                f"secret of at least {MIN_SECRET_LENGTH} characters"
            )
        material = value
    else:
        material = value or DEFAULT_ENCRYPTION_KEY
    return _derive_aes_key(material)


def _derive_aes_key(material: str | bytes) -> bytes:
    raw = material.encode() if isinstance(material, str) else material
    # Normalise arbitrary-length material to a 32-byte AES-256 key.
    return hashlib.sha256(raw).digest()


def _key_id(key: bytes) -> str:
    return hashlib.sha256(b"swarmguard-key-id" + key).hexdigest()[:16]


def encrypt_sensitive(plaintext: str, *, key: bytes | None = None, getenv: Getenv | None = None) -> dict[str, str]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aes_key = key if key is not None else resolve_encryption_key(getenv)
    if len(aes_key) != 32:
        aes_key = _derive_aes_key(aes_key)
    nonce = os.urandom(12)
    ciphertext = AESGCM(aes_key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return {
        "alg": ENCRYPTION_ALG,
        "key_id": _key_id(aes_key),
        "nonce": _b64url_encode(nonce),
        "ciphertext": _b64url_encode(ciphertext),
    }


def decrypt_sensitive(envelope: dict[str, str], *, key: bytes | None = None, getenv: Getenv | None = None) -> str:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aes_key = key if key is not None else resolve_encryption_key(getenv)
    if len(aes_key) != 32:
        aes_key = _derive_aes_key(aes_key)
    if envelope.get("alg") != ENCRYPTION_ALG:
        raise DecryptionError(f"unsupported encryption alg: {envelope.get('alg')!r}")
    try:
        nonce = _b64url_decode(envelope["nonce"])
        ciphertext = _b64url_decode(envelope["ciphertext"])
    except (KeyError, ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise DecryptionError("malformed ciphertext envelope") from exc
    try:
        return AESGCM(aes_key).decrypt(nonce, ciphertext, None).decode("utf-8")
    except InvalidTag as exc:
        raise DecryptionError("ciphertext authentication failed") from exc


# ---------------------------------------------------------------------------
# Network exposure (CORS / bind host)
# ---------------------------------------------------------------------------


def resolve_cors_origins(getenv: Getenv | None = None) -> list[str]:
    getenv = _getenv(getenv)
    raw = getenv("SWARMGUARD_CORS_ORIGINS")
    if is_production(getenv):
        origins = [item.strip() for item in (raw or "").split(",") if item.strip()]
        if not origins or "*" in origins:
            raise SecurityConfigError(
                "production requires SWARMGUARD_CORS_ORIGINS to be an explicit, "
                "non-wildcard allowlist"
            )
        return origins
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return ["*"]


def resolve_api_bind_host(getenv: Getenv | None = None) -> str:
    getenv = _getenv(getenv)
    host = getenv("API_HOST") or "0.0.0.0"
    if is_production(getenv) and host in {"0.0.0.0", "::", ""}:
        raise SecurityConfigError(
            "production must not bind the API to a wildcard address; set API_HOST "
            "to a specific interface (e.g. 127.0.0.1 behind a reverse proxy)"
        )
    return host


# ---------------------------------------------------------------------------
# Access audit records
# ---------------------------------------------------------------------------


def access_audit_record(
    *,
    method: str,
    path: str,
    subject: str | None,
    role: str | None,
    decision: str,
    status: int,
    client: str | None = None,
) -> dict[str, Any]:
    """Build a structured access-audit record.

    The record deliberately never carries bearer tokens or other secrets, only
    the already-verified subject/role and the request/response metadata.
    """
    record = {
        "type": "access_audit",
        "ts": time.time(),
        "method": method,
        "path": path,
        "subject": subject,
        "role": role,
        "decision": decision,
        "status": status,
    }
    if client is not None:
        record["client"] = client
    return redact(record)
