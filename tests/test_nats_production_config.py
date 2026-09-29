from __future__ import annotations

from pathlib import Path

PROD_CONFIG = Path(__file__).resolve().parents[1] / "infra" / "nats" / "nats-production.conf"


def user_block(config: str, user: str) -> str:
    marker = f"# identity: {user}"
    start = config.index(marker)
    next_user = config.find("# identity:", start + len(marker))
    return config[start:] if next_user == -1 else config[start:next_user]


def test_production_config_exists() -> None:
    assert PROD_CONFIG.is_file()


def test_production_config_requires_tls_with_verification() -> None:
    config = PROD_CONFIG.read_text(encoding="utf-8")
    assert "tls {" in config
    assert "cert_file" in config
    assert "key_file" in config
    assert "ca_file" in config
    assert "verify: true" in config


def test_production_config_uses_nkey_or_credentials_not_dev_passwords() -> None:
    config = PROD_CONFIG.read_text(encoding="utf-8")
    # No checked-in development passwords may leak into the production template.
    for leaked in ["gateway-dev", "tools-dev", "researcher-dev", "projector-dev", "telemetry-dev"]:
        assert leaked not in config
    # Least-privilege identities must be keyed by NKey public keys, not passwords.
    assert "nkey:" in config
    assert "password:" not in config


def test_production_config_has_dedicated_timeline_identity_scoped_to_audit_feed() -> None:
    config = PROD_CONFIG.read_text(encoding="utf-8")
    timeline = user_block(config, "timeline")
    # Can consume/bind the audit live feed...
    assert "audit.>" in timeline
    assert "$JS.API.CONSUMER" in timeline
    # ...but must never be able to publish to private tool workers.
    assert "private.tool" not in timeline


def test_production_config_preserves_least_privilege_agent_publish_scope() -> None:
    config = PROD_CONFIG.read_text(encoding="utf-8")
    for user in ["researcher", "analyst", "operator"]:
        block = user_block(config, user)
        assert "audit.>" not in block
        assert f"swarm.*.agent.{user}.tool.request" in block


def test_production_config_gateway_keeps_tool_and_audit_scope() -> None:
    config = PROD_CONFIG.read_text(encoding="utf-8")
    gateway = user_block(config, "gateway")
    assert "audit.>" in gateway
    assert "private.tool.>" in gateway


def test_readme_documents_production_security_and_limitations() -> None:
    readme = (PROD_CONFIG.parents[2] / "README.md").read_text(encoding="utf-8")
    assert "Production security configuration" in readme
    assert "SWARMGUARD_ENV" in readme
    assert "SWARMGUARD_API_SIGNING_KEY" in readme
    assert "SWARMGUARD_AUDIT_ENCRYPTION_KEY" in readme
    assert "SWARMGUARD_CORS_ORIGINS" in readme
    assert "AES-256-GCM" in readme
    assert "DNS rebinding" in readme
    assert "Limitations" in readme
    # Honesty about the sandbox boundary is required.
    assert "seccomp" in readme
    assert "namespaces" in readme
