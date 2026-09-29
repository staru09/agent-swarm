from __future__ import annotations

import json
import time

import pytest

from swarmguard import security


# ---------------------------------------------------------------------------
# Environment mode
# ---------------------------------------------------------------------------


def test_environment_defaults_to_development(monkeypatch) -> None:
    monkeypatch.delenv("SWARMGUARD_ENV", raising=False)
    assert security.get_environment() == "development"
    assert security.is_production() is False


def test_environment_reads_production(monkeypatch) -> None:
    monkeypatch.setenv("SWARMGUARD_ENV", "production")
    assert security.get_environment() == "production"
    assert security.is_production() is True


def test_environment_rejects_unknown_value(monkeypatch) -> None:
    monkeypatch.setenv("SWARMGUARD_ENV", "staging")
    with pytest.raises(security.SecurityConfigError):
        security.get_environment()


# ---------------------------------------------------------------------------
# Roles / RBAC ladder
# ---------------------------------------------------------------------------


def test_role_ladder_orders_viewer_operator_admin() -> None:
    assert security.role_satisfies("admin", "viewer") is True
    assert security.role_satisfies("operator", "viewer") is True
    assert security.role_satisfies("operator", "operator") is True
    assert security.role_satisfies("viewer", "operator") is False
    assert security.role_satisfies("viewer", "admin") is False
    assert security.role_satisfies("operator", "admin") is False


def test_role_satisfies_rejects_unknown_role() -> None:
    with pytest.raises(security.SecurityConfigError):
        security.role_satisfies("superuser", "viewer")


# ---------------------------------------------------------------------------
# Bearer tokens (HMAC signed, short-lived)
# ---------------------------------------------------------------------------


def test_mint_and_verify_round_trips_claims() -> None:
    token = security.mint_token("alice", "operator", key="k" * 32, ttl_seconds=900)
    claims = security.verify_token(token, key="k" * 32)
    assert claims.sub == "alice"
    assert claims.role == "operator"
    assert claims.iss == security.TOKEN_ISSUER
    assert claims.aud == security.TOKEN_AUDIENCE


def test_verify_rejects_bad_signature() -> None:
    token = security.mint_token("alice", "viewer", key="k" * 32)
    with pytest.raises(security.AuthError):
        security.verify_token(token, key="different-key-abcdefghijklmnop")


def test_verify_rejects_expired_token() -> None:
    now = time.time()
    token = security.mint_token("alice", "viewer", key="k" * 32, ttl_seconds=1, now=now - 10)
    with pytest.raises(security.AuthError):
        security.verify_token(token, key="k" * 32, now=now)


def test_verify_rejects_wrong_issuer_or_audience() -> None:
    token = security.mint_token("alice", "viewer", key="k" * 32)
    with pytest.raises(security.AuthError):
        security.verify_token(token, key="k" * 32, audience="other-audience")
    with pytest.raises(security.AuthError):
        security.verify_token(token, key="k" * 32, issuer="other-issuer")


def test_verify_rejects_unknown_role_claim() -> None:
    # Minting with an invalid role must be blocked at mint time.
    with pytest.raises(security.SecurityConfigError):
        security.mint_token("alice", "root", key="k" * 32)
    # A structurally valid token carrying an unknown role must fail verification.
    forged = security._encode_token({"sub": "x", "role": "root", "iss": security.TOKEN_ISSUER,
                                     "aud": security.TOKEN_AUDIENCE, "iat": time.time(),
                                     "exp": time.time() + 60}, ("k" * 32).encode())
    with pytest.raises(security.AuthError):
        security.verify_token(forged, key="k" * 32)


def test_verify_rejects_malformed_token() -> None:
    with pytest.raises(security.AuthError):
        security.verify_token("not-a-token", key="k" * 32)


def test_production_requires_strong_non_default_auth_key(monkeypatch) -> None:
    monkeypatch.setenv("SWARMGUARD_ENV", "production")
    monkeypatch.delenv("SWARMGUARD_API_SIGNING_KEY", raising=False)
    with pytest.raises(security.SecurityConfigError):
        security.resolve_auth_signing_key()
    monkeypatch.setenv("SWARMGUARD_API_SIGNING_KEY", "short")
    with pytest.raises(security.SecurityConfigError):
        security.resolve_auth_signing_key()
    monkeypatch.setenv("SWARMGUARD_API_SIGNING_KEY", "x" * 32)
    assert security.resolve_auth_signing_key() == b"x" * 32


def test_development_auth_key_has_working_default(monkeypatch) -> None:
    monkeypatch.delenv("SWARMGUARD_ENV", raising=False)
    monkeypatch.delenv("SWARMGUARD_API_SIGNING_KEY", raising=False)
    assert security.resolve_auth_signing_key() == security.DEFAULT_AUTH_SIGNING_KEY.encode()


# ---------------------------------------------------------------------------
# Harness signing key
# ---------------------------------------------------------------------------


def test_harness_key_dev_default(monkeypatch) -> None:
    monkeypatch.delenv("SWARMGUARD_ENV", raising=False)
    monkeypatch.delenv("SWARMGUARD_HARNESS_SIGNING_KEY", raising=False)
    assert security.resolve_harness_signing_key() == security.DEFAULT_HARNESS_SIGNING_KEY


def test_harness_key_production_fails_closed_on_default(monkeypatch) -> None:
    monkeypatch.setenv("SWARMGUARD_ENV", "production")
    monkeypatch.delenv("SWARMGUARD_HARNESS_SIGNING_KEY", raising=False)
    with pytest.raises(security.SecurityConfigError):
        security.resolve_harness_signing_key()
    monkeypatch.setenv("SWARMGUARD_HARNESS_SIGNING_KEY", security.DEFAULT_HARNESS_SIGNING_KEY)
    with pytest.raises(security.SecurityConfigError):
        security.resolve_harness_signing_key()
    monkeypatch.setenv("SWARMGUARD_HARNESS_SIGNING_KEY", "weak")
    with pytest.raises(security.SecurityConfigError):
        security.resolve_harness_signing_key()
    monkeypatch.setenv("SWARMGUARD_HARNESS_SIGNING_KEY", "s" * 32)
    assert security.resolve_harness_signing_key() == "s" * 32


# ---------------------------------------------------------------------------
# Secret classification / redaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "apiKey",
        "authorization",
        "token",
        "access_token",
        "password",
        "secret",
        "client_secret",
        "credentials",
        "private_key",
        "ANTHROPIC_API_KEY",
    ],
)
def test_sensitive_keys_are_classified(key: str) -> None:
    assert security.is_sensitive_key(key) is True


@pytest.mark.parametrize("key", ["url", "path", "command", "message", "tool", "run_id"])
def test_non_sensitive_keys_are_not_classified(key: str) -> None:
    assert security.is_sensitive_key(key) is False


def test_redact_recurses_into_nested_structures() -> None:
    payload = {
        "arguments": {
            "url": "https://wikipedia.org",
            "headers": {"Authorization": "Bearer sk-secret-123"},
        },
        "items": [
            {"password": "hunter2", "note": "ok"},
            {"nested": {"api_key": "sk-abc"}},
        ],
        "tool": "web_lookup",
    }
    redacted = security.redact(payload)
    text = json.dumps(redacted)
    assert "sk-secret-123" not in text
    assert "hunter2" not in text
    assert "sk-abc" not in text
    assert redacted["arguments"]["url"] == "https://wikipedia.org"
    assert redacted["tool"] == "web_lookup"
    assert redacted["items"][0]["note"] == "ok"
    assert redacted["arguments"]["headers"]["Authorization"] == security.REDACTION_PLACEHOLDER


def test_redact_scrubs_secret_looking_string_values() -> None:
    payload = {"note": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"}
    redacted = security.redact(payload)
    assert "sk-ant-api03" not in json.dumps(redacted)


def test_redact_does_not_mutate_input() -> None:
    payload = {"password": "hunter2"}
    security.redact(payload)
    assert payload["password"] == "hunter2"


# ---------------------------------------------------------------------------
# Encryption of retained sensitive payloads
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_round_trip() -> None:
    key = b"k" * 32
    envelope = security.encrypt_sensitive("super-secret-value", key=key)
    assert set(envelope) >= {"alg", "key_id", "nonce", "ciphertext"}
    assert envelope["alg"] == "AES-256-GCM"
    assert "super-secret-value" not in json.dumps(envelope)
    assert security.decrypt_sensitive(envelope, key=key) == "super-secret-value"


def test_tampered_ciphertext_fails_decryption() -> None:
    key = b"k" * 32
    envelope = security.encrypt_sensitive("super-secret-value", key=key)
    tampered = dict(envelope)
    raw = bytearray(security._b64url_decode(tampered["ciphertext"]))
    raw[0] ^= 0xFF
    tampered["ciphertext"] = security._b64url_encode(bytes(raw))
    with pytest.raises(security.DecryptionError):
        security.decrypt_sensitive(tampered, key=key)


def test_decrypt_with_wrong_key_fails() -> None:
    envelope = security.encrypt_sensitive("value", key=b"a" * 32)
    with pytest.raises(security.DecryptionError):
        security.decrypt_sensitive(envelope, key=b"b" * 32)


def test_production_requires_encryption_key_from_env(monkeypatch) -> None:
    monkeypatch.setenv("SWARMGUARD_ENV", "production")
    monkeypatch.delenv("SWARMGUARD_AUDIT_ENCRYPTION_KEY", raising=False)
    with pytest.raises(security.SecurityConfigError):
        security.resolve_encryption_key()
    monkeypatch.setenv("SWARMGUARD_AUDIT_ENCRYPTION_KEY", "x" * 32)
    assert len(security.resolve_encryption_key()) == 32


# ---------------------------------------------------------------------------
# CORS allowlist
# ---------------------------------------------------------------------------


def test_cors_dev_allows_wildcard(monkeypatch) -> None:
    monkeypatch.delenv("SWARMGUARD_ENV", raising=False)
    monkeypatch.delenv("SWARMGUARD_CORS_ORIGINS", raising=False)
    assert security.resolve_cors_origins() == ["*"]


def test_cors_production_requires_explicit_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("SWARMGUARD_ENV", "production")
    monkeypatch.delenv("SWARMGUARD_CORS_ORIGINS", raising=False)
    with pytest.raises(security.SecurityConfigError):
        security.resolve_cors_origins()
    monkeypatch.setenv("SWARMGUARD_CORS_ORIGINS", "*")
    with pytest.raises(security.SecurityConfigError):
        security.resolve_cors_origins()
    monkeypatch.setenv("SWARMGUARD_CORS_ORIGINS", "https://a.example, https://b.example")
    assert security.resolve_cors_origins() == ["https://a.example", "https://b.example"]


# ---------------------------------------------------------------------------
# API bind hardening
# ---------------------------------------------------------------------------


def test_bind_host_production_rejects_wildcard(monkeypatch) -> None:
    monkeypatch.setenv("SWARMGUARD_ENV", "production")
    monkeypatch.setenv("API_HOST", "0.0.0.0")
    with pytest.raises(security.SecurityConfigError):
        security.resolve_api_bind_host()
    monkeypatch.setenv("API_HOST", "127.0.0.1")
    assert security.resolve_api_bind_host() == "127.0.0.1"


def test_bind_host_dev_allows_wildcard(monkeypatch) -> None:
    monkeypatch.delenv("SWARMGUARD_ENV", raising=False)
    monkeypatch.setenv("API_HOST", "0.0.0.0")
    assert security.resolve_api_bind_host() == "0.0.0.0"
