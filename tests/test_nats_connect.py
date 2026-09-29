from __future__ import annotations

import ssl

import pytest

from swarmguard import bus, security


def test_dev_connect_options_allow_password_and_plain_url(monkeypatch) -> None:
    monkeypatch.delenv("SWARMGUARD_ENV", raising=False)
    monkeypatch.setenv("NATS_URL", "nats://127.0.0.1:4222")
    monkeypatch.delenv("NATS_CREDS", raising=False)
    monkeypatch.setenv("NATS_USER", "gateway")
    monkeypatch.setenv("NATS_PASSWORD", "gateway-dev")

    options = bus.build_nats_options("policy-gateway")

    assert options["servers"] == ["nats://127.0.0.1:4222"]
    assert options["user"] == "gateway"
    assert options["password"] == "gateway-dev"
    assert "tls" not in options


def test_production_requires_tls_url(monkeypatch) -> None:
    monkeypatch.setenv("SWARMGUARD_ENV", "production")
    monkeypatch.setenv("NATS_URL", "nats://nats.internal:4222")
    monkeypatch.setenv("NATS_CREDS", "/nonexistent/creds.creds")
    with pytest.raises(security.SecurityConfigError):
        bus.build_nats_options("policy-gateway")


def test_production_requires_credentials_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SWARMGUARD_ENV", "production")
    monkeypatch.setenv("NATS_URL", "tls://nats.internal:4222")
    monkeypatch.delenv("NATS_CREDS", raising=False)
    with pytest.raises(security.SecurityConfigError):
        bus.build_nats_options("policy-gateway")

    missing = tmp_path / "missing.creds"
    monkeypatch.setenv("NATS_CREDS", str(missing))
    with pytest.raises(security.SecurityConfigError):
        bus.build_nats_options("policy-gateway")


def test_production_rejects_password_fallback(monkeypatch, tmp_path) -> None:
    creds = tmp_path / "user.creds"
    creds.write_text("-----BEGIN NATS USER JWT-----\nx\n------END NATS USER JWT------\n", encoding="utf-8")
    monkeypatch.setenv("SWARMGUARD_ENV", "production")
    monkeypatch.setenv("NATS_URL", "tls://nats.internal:4222")
    monkeypatch.setenv("NATS_CREDS", str(creds))
    monkeypatch.setenv("NATS_PASSWORD", "should-not-be-used")

    options = bus.build_nats_options("policy-gateway")

    assert options["user_credentials"] == str(creds)
    assert "password" not in options
    assert "user" not in options
    assert isinstance(options["tls"], ssl.SSLContext)


def test_production_supports_ca_cert_key_verification(monkeypatch, tmp_path) -> None:
    creds = tmp_path / "user.creds"
    creds.write_text("creds", encoding="utf-8")
    ca = tmp_path / "ca.pem"
    ca.write_text("ca", encoding="utf-8")
    monkeypatch.setenv("SWARMGUARD_ENV", "production")
    monkeypatch.setenv("NATS_URL", "tls://nats.internal:4222")
    monkeypatch.setenv("NATS_CREDS", str(creds))
    monkeypatch.setenv("NATS_CA", str(ca))

    called = {}

    def fake_load_verify(self, cafile=None, **kwargs):  # noqa: ANN001
        called["ca"] = cafile

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", fake_load_verify)

    options = bus.build_nats_options("timeline-api")
    assert called["ca"] == str(ca)
    assert isinstance(options["tls"], ssl.SSLContext)
    assert options["tls_hostname"] == "nats.internal"
