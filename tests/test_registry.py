from __future__ import annotations

from pathlib import Path

import pytest

from swarmguard.registry import ConfigError, load_agent_registry, load_tool_registry


ROOT = Path(__file__).resolve().parents[1]


def test_default_agent_registry_loads_demo_agents_and_disabled_extension() -> None:
    registry = load_agent_registry(ROOT / "config" / "agents.yaml")

    assert registry.enabled_agent_ids() == [
        "analyst",
        "hypothesis-generator",
        "operator",
        "paper-reviewer",
        "researcher",
        "summary-writer",
        "web-researcher",
    ]
    cartographer = registry.resolve_agent("cartographer")
    assert cartographer.enabled is False
    assert cartographer.default_task == "Summarize the available map metadata using echo_metadata."
    assert cartographer.policy_binding == "cartographer"
    assert cartographer.credential_ref == "cartographer"


def test_default_tool_registry_loads_existing_tools_and_extension_manifest() -> None:
    registry = load_tool_registry(ROOT / "config" / "tools.yaml")

    dispatchable = [name for name in registry.tool_names() if registry.resolve_tool(name).dispatchable]
    assert dispatchable == [
        "candidate_read",
        "echo_metadata",
        "exa_paper_search",
        "future_directions_write",
        "literature_summary_read",
        "literature_summary_write",
        "reviewed_papers_read",
        "reviewed_papers_write",
        "safe_shell",
        "web_lookup",
        "workspace_read",
    ]
    assert all(registry.resolve_tool(name).classification == "decoy" for name in set(registry.tool_names()) - set(dispatchable))
    web_lookup = registry.resolve_tool("web_lookup")
    assert web_lookup.worker_subject == "private.tool.web_lookup.execute"
    assert web_lookup.input_schema["required"] == ["url"]
    assert web_lookup.output_schema["type"] in {"object", "string"}

    extension = registry.resolve_tool("echo_metadata")
    assert extension.dispatchable is True
    assert extension.classification == "normal"
    assert extension.worker_capability == "echo"
    assert extension.retry_policy.max_attempts == 1


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            """
tools:
  - version: 99
    name: bad_version
    description: bad
    input_schema: {type: object}
    output_schema: {type: object}
    sensitivity: low
    timeout_seconds: 1
    retry_policy: {max_attempts: 1}
    worker: {subject: private.tool.bad_version.execute, capability: bad}
    resource_limits: {max_output_bytes: 1000}
    classification: normal
    dispatchable: true
""",
            "unsupported tool manifest version",
        ),
        (
            """
tools:
  - version: 1
    name: unsafe.tool
    description: bad
    input_schema: {type: object}
    output_schema: {type: object}
    sensitivity: low
    timeout_seconds: 1
    retry_policy: {max_attempts: 1}
    worker: {subject: private.tool.unsafe.tool.execute, capability: bad}
    resource_limits: {max_output_bytes: 1000}
    classification: normal
    dispatchable: true
""",
            "unsafe tool token",
        ),
        (
            """
tools:
  - version: 1
    name: decoy_probe
    description: bad
    input_schema: {type: object}
    output_schema: {type: object}
    sensitivity: low
    timeout_seconds: 1
    retry_policy: {max_attempts: 1}
    worker: {subject: private.tool.decoy_probe.execute, capability: bad}
    resource_limits: {max_output_bytes: 1000}
    classification: decoy
    dispatchable: true
""",
            "decoy tools must not be dispatchable",
        ),
        (
            """
tools:
  - version: 1
    name: zero_timeout
    description: bad
    input_schema: {type: object}
    output_schema: {type: object}
    sensitivity: low
    timeout_seconds: 0
    retry_policy: {max_attempts: 1}
    worker: {subject: private.tool.zero_timeout.execute, capability: bad}
    resource_limits: {max_output_bytes: 1000}
    classification: normal
    dispatchable: true
""",
            "timeout_seconds must be positive",
        ),
        (
            """
tools:
  - version: 1
    name: bad_schema
    description: bad
    input_schema: {type: string}
    output_schema: {type: object}
    sensitivity: low
    timeout_seconds: 1
    retry_policy: {max_attempts: 1}
    worker: {subject: private.tool.bad_schema.execute, capability: bad}
    resource_limits: {max_output_bytes: 1000}
    classification: normal
    dispatchable: true
""",
            "input_schema must be an object schema",
        ),
    ],
)
def test_invalid_tool_manifests_are_rejected(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "tools.yaml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_tool_registry(path)


def test_duplicate_agent_ids_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "agents.yaml"
    path.write_text(
        """
agents:
  - agent_id: duplicate
    role_prompt: first
    harness: {provider: anthropic}
    policy_binding: duplicate
    credential_ref: duplicate
    enabled: true
  - agent_id: duplicate
    role_prompt: second
    harness: {provider: anthropic}
    policy_binding: duplicate
    credential_ref: duplicate
    enabled: true
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="duplicate agent_id"):
        load_agent_registry(path)


@pytest.mark.parametrize("enabled", ['"false"', "1", "null"])
def test_agent_enabled_must_be_a_strict_boolean(tmp_path: Path, enabled: str) -> None:
    path = tmp_path / "agents.yaml"
    path.write_text(
        f"""
agents:
  - agent_id: loose_agent
    role_prompt: prompt
    harness: {{provider: anthropic}}
    policy_binding: loose_agent
    credential_ref: loose_agent
    enabled: {enabled}
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="enabled must be a boolean"):
        load_agent_registry(path)


@pytest.mark.parametrize("dispatchable", ['"true"', "1", "null"])
def test_tool_dispatchable_must_be_a_strict_boolean(tmp_path: Path, dispatchable: str) -> None:
    path = tmp_path / "tools.yaml"
    path.write_text(
        f"""
tools:
  - version: 1
    name: loose_tool
    description: bad
    input_schema: {{type: object}}
    output_schema: {{type: object}}
    sensitivity: low
    timeout_seconds: 1
    retry_policy: {{max_attempts: 1}}
    worker: {{subject: private.tool.loose_tool.execute, capability: bad}}
    resource_limits: {{max_output_bytes: 1000}}
    classification: normal
    dispatchable: {dispatchable}
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="dispatchable must be a boolean"):
        load_tool_registry(path)
