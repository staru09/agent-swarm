from __future__ import annotations

from pathlib import Path

from swarmguard.protocol import tool_cancel_subject


NATS_CONFIG = Path(__file__).resolve().parents[1] / "infra" / "nats" / "nats.conf"


def user_block(config: str, user: str) -> str:
    marker = f'user: "{user}"'
    start = config.index(marker)
    next_user = config.find("user:", start + len(marker))
    return config[start:] if next_user == -1 else config[start:next_user]


def test_nats_acl_allows_gateway_publish_and_tools_subscribe_to_tool_call_cancellation_subject() -> None:
    config = NATS_CONFIG.read_text(encoding="utf-8")
    cancel_subject = tool_cancel_subject("safe_shell", "tool-call-1")
    acl_pattern = "private.tool_call.*.*.cancel"

    # Subject is scoped by tool then tool_call_id; the ACL pattern must match that exact shape.
    assert cancel_subject == "private.tool_call.safe_shell.tool-call-1.cancel"
    assert len(acl_pattern.split(".")) == len(cancel_subject.split("."))
    assert acl_pattern in user_block(config, "gateway")
    assert acl_pattern in user_block(config, "tools")
    # The superseded, under-scoped pattern must be gone so ACLs stay exactly aligned.
    assert "private.tool_call.*.cancel" not in config


def test_nats_config_limits_and_acls_align_with_declared_stream_topology() -> None:
    config = NATS_CONFIG.read_text(encoding="utf-8")

    assert "max_mem_store: 512MB" in config
    assert "max_file_store: 5GB" in config
    assert "max_payload: 2MB" in config

    gateway = user_block(config, "gateway")
    tools = user_block(config, "tools")
    bootstrap = user_block(config, "stream-bootstrap")
    projector = user_block(config, "projector")
    assert 'publish: ["audit.>", "commands.>", "retry.>", "deadletter.>", "private.tool.>", "private.tool_call.*.*.cancel", "_INBOX.>"]' in gateway
    assert 'publish: ["audit.>", "deadletter.>", "_INBOX.>"]' in tools
    assert 'publish: ["$JS.API.STREAM.CREATE.SWARMGUARD_AUDIT", "$JS.API.STREAM.CREATE.SWARMGUARD_COMMANDS", "$JS.API.STREAM.CREATE.SWARMGUARD_RETRY", "$JS.API.STREAM.CREATE.SWARMGUARD_DEAD_LETTER", "$JS.API.STREAM.UPDATE.*", "$JS.API.STREAM.INFO.*", "$JS.API.CONSUMER.DURABLE.CREATE.SWARMGUARD_AUDIT.swarmguard-projector-v1", "$JS.API.CONSUMER.INFO.SWARMGUARD_AUDIT.swarmguard-projector-v1"]' in bootstrap
    assert 'subscribe: ["_INBOX.>"]' in bootstrap
    assert 'publish: ["$JS.API.CONSUMER.INFO.SWARMGUARD_AUDIT.swarmguard-projector-v1", "$JS.API.CONSUMER.MSG.NEXT.SWARMGUARD_AUDIT.swarmguard-projector-v1", "$JS.ACK.>", "deadletter.audit.projector"]' in projector
    assert 'subscribe: ["_INBOX.>"]' in projector
    assert "$JS.API.>" not in bootstrap
    assert "$JS.API.>" not in projector
    assert "$JS.API.CONSUMER.CREATE.SWARMGUARD_AUDIT.swarmguard-projector-v1" not in bootstrap
    assert "$JS.API.CONSUMER.DURABLE.CREATE" not in projector
    assert 'subscribe: ["audit.>"' not in projector


def test_agents_cannot_publish_audit_directly_and_gateway_subscribes_to_lifecycle() -> None:
    config = NATS_CONFIG.read_text(encoding="utf-8")

    for user in ["researcher", "analyst", "operator"]:
        block = user_block(config, user)
        assert "audit.>" not in block
        assert f'"swarm.*.agent.{user}.lifecycle"' in block
    gateway = user_block(config, "gateway")
    assert '"swarm.*.agent.*.lifecycle"' in gateway


def test_readme_documents_least_privilege_jetstream_credentials() -> None:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")

    assert "NATS_USER=stream-bootstrap NATS_PASSWORD=stream-bootstrap-dev swarmguard-stream-bootstrap" in readme
    assert "NATS_USER=projector NATS_PASSWORD=projector-dev swarmguard-projector" in readme


def test_readme_documents_lifecycle_hmac_and_delivery_limitations() -> None:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")

    assert "SWARMGUARD_HARNESS_SIGNING_KEY" in readme
    assert "shared development HMAC default" in readme
    assert "at-most-once lifecycle hop" in readme
