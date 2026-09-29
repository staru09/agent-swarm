from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft7Validator, SchemaError


CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"
DEFAULT_AGENTS_PATH = CONFIG_ROOT / "agents.yaml"
DEFAULT_TOOLS_PATH = CONFIG_ROOT / "tools.yaml"
SUPPORTED_TOOL_MANIFEST_VERSIONS = {"1", 1}
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AgentRegistration:
    agent_id: str
    role_prompt: str
    harness: dict[str, Any]
    policy_binding: str
    credential_ref: str
    enabled: bool
    default_task: str | None = None
    tools: tuple[str, ...] = ()
    decoy_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int


@dataclass(frozen=True)
class ResourceLimits:
    max_output_bytes: int
    max_cpu_seconds: int | None = None
    max_memory_bytes: int | None = None
    max_file_size_bytes: int | None = None
    max_open_files: int | None = None


@dataclass(frozen=True)
class ToolManifest:
    version: str
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    sensitivity: str
    timeout_seconds: float
    retry_policy: RetryPolicy
    worker_subject: str
    worker_capability: str
    resource_limits: ResourceLimits
    classification: str
    dispatchable: bool
    network: bool = False
    env: tuple[str, ...] = ()

    def anthropic_tool(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class AgentRegistry:
    def __init__(self, registrations: list[AgentRegistration]):
        self._agents = {registration.agent_id: registration for registration in registrations}

    def resolve_agent(self, agent_id: str) -> AgentRegistration:
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise ConfigError(f"unknown agent_id: {agent_id}") from exc

    def agent_ids(self) -> list[str]:
        return sorted(self._agents)

    def enabled_agent_ids(self) -> list[str]:
        return sorted(agent_id for agent_id, agent in self._agents.items() if agent.enabled)

    def enabled_agents(self) -> list[AgentRegistration]:
        return [self._agents[agent_id] for agent_id in self.enabled_agent_ids()]


class ToolRegistry:
    def __init__(self, manifests: list[ToolManifest]):
        self._tools = {manifest.name: manifest for manifest in manifests}

    def resolve_tool(self, name: str) -> ToolManifest:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ConfigError(f"unknown tool manifest: {name}") from exc

    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def anthropic_tools(self, names: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        if names:
            return [self.resolve_tool(name).anthropic_tool() for name in names]
        return [
            self._tools[name].anthropic_tool()
            for name in self.tool_names()
            if self._tools[name].dispatchable and self._tools[name].classification == "normal"
        ]


def load_agent_registry(path: str | Path = DEFAULT_AGENTS_PATH) -> AgentRegistry:
    raw = _load_yaml(Path(path))
    agents = raw.get("agents")
    if not isinstance(agents, list):
        raise ConfigError("agents must be a list")
    seen: set[str] = set()
    registrations: list[AgentRegistration] = []
    for item in agents:
        if not isinstance(item, dict):
            raise ConfigError("agent registration must be a mapping")
        agent_id = _required_str(item, "agent_id")
        _validate_token(agent_id, "agent_id")
        if agent_id in seen:
            raise ConfigError(f"duplicate agent_id: {agent_id}")
        seen.add(agent_id)
        policy_binding = _required_str(item, "policy_binding")
        credential_ref = _required_str(item, "credential_ref")
        _validate_token(policy_binding, "policy_binding")
        _validate_token(credential_ref, "credential_ref")
        harness = item.get("harness")
        if not isinstance(harness, dict):
            raise ConfigError(f"harness must be a mapping for agent_id: {agent_id}")
        registrations.append(
            AgentRegistration(
                agent_id=agent_id,
                role_prompt=_required_str(item, "role_prompt"),
                harness=dict(harness),
                policy_binding=policy_binding,
                credential_ref=credential_ref,
                enabled=_required_bool(item, "enabled"),
                default_task=item.get("default_task"),
                tools=_token_list(item, "tools"),
                decoy_tools=_token_list(item, "decoy_tools"),
            )
        )
    return AgentRegistry(registrations)


def load_tool_registry(path: str | Path = DEFAULT_TOOLS_PATH) -> ToolRegistry:
    raw = _load_yaml(Path(path))
    tools = raw.get("tools")
    if not isinstance(tools, list):
        raise ConfigError("tools must be a list")
    seen: set[str] = set()
    manifests: list[ToolManifest] = []
    for item in tools:
        if not isinstance(item, dict):
            raise ConfigError("tool manifest must be a mapping")
        name = _required_str(item, "name")
        _validate_token(name, "tool")
        if name in seen:
            raise ConfigError(f"duplicate tool name: {name}")
        seen.add(name)
        version = item.get("version")
        if version not in SUPPORTED_TOOL_MANIFEST_VERSIONS:
            raise ConfigError(f"unsupported tool manifest version: {version}")
        input_schema = _schema(item, "input_schema", require_object=True)
        output_schema = _schema(item, "output_schema", require_object=False)
        timeout_seconds = _positive_number(item, "timeout_seconds")
        retry_policy = item.get("retry_policy")
        if not isinstance(retry_policy, dict):
            raise ConfigError("retry_policy must be a mapping")
        max_attempts = _positive_int(retry_policy, "max_attempts")
        classification = _required_str(item, "classification")
        if classification not in {"normal", "decoy"}:
            raise ConfigError(f"unsupported classification: {classification}")
        dispatchable = _required_bool(item, "dispatchable")
        if classification == "decoy" and dispatchable:
            raise ConfigError("decoy tools must not be dispatchable")
        worker = item.get("worker")
        if classification == "decoy":
            # Decoys are catalog-only: they must never name a worker to dispatch to.
            if worker is not None:
                raise ConfigError("decoy tools must not define a worker")
            worker = {"capability": "none"}
        if not isinstance(worker, dict):
            raise ConfigError("worker must be a mapping")
        worker_subject = ""
        if classification != "decoy":
            worker_subject = _required_str(worker, "subject")
            _validate_subject(worker_subject)
        worker_capability = _required_str(worker, "capability")
        _validate_token(worker_capability, "worker capability")
        network = worker.get("network", False)
        if type(network) is not bool:
            raise ConfigError("worker network must be a boolean")
        env = _token_list(worker, "env")
        resource_limits = item.get("resource_limits")
        if not isinstance(resource_limits, dict):
            raise ConfigError("resource_limits must be a mapping")
        max_output_bytes = _positive_int(resource_limits, "max_output_bytes")
        max_cpu_seconds = _optional_positive_int(resource_limits, "max_cpu_seconds")
        max_memory_bytes = _optional_positive_int(resource_limits, "max_memory_bytes")
        max_file_size_bytes = _optional_positive_int(resource_limits, "max_file_size_bytes")
        max_open_files = _optional_positive_int(resource_limits, "max_open_files")
        manifests.append(
            ToolManifest(
                version=str(version),
                name=name,
                description=_required_str(item, "description"),
                input_schema=input_schema,
                output_schema=output_schema,
                sensitivity=_required_str(item, "sensitivity"),
                timeout_seconds=timeout_seconds,
                retry_policy=RetryPolicy(max_attempts=max_attempts),
                worker_subject=worker_subject,
                worker_capability=worker_capability,
                resource_limits=ResourceLimits(
                    max_output_bytes=max_output_bytes,
                    max_cpu_seconds=max_cpu_seconds,
                    max_memory_bytes=max_memory_bytes,
                    max_file_size_bytes=max_file_size_bytes,
                    max_open_files=max_open_files,
                ),
                classification=classification,
                dispatchable=dispatchable,
                network=network,
                env=env,
            )
        )
    return ToolRegistry(manifests)


def validate_json(schema: dict[str, Any], payload: Any, *, label: str) -> None:
    errors = sorted(Draft7Validator(schema).iter_errors(payload), key=lambda error: list(error.path))
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.path) or "<root>"
        raise ConfigError(f"{label} schema validation failed at {location}: {first.message}")


def enforce_output_limit(payload: Any, max_output_bytes: int) -> None:
    size = len(json.dumps(payload, sort_keys=True, default=str).encode("utf-8"))
    if size > max_output_bytes:
        raise ConfigError(f"output exceeds max_output_bytes: {size} > {max_output_bytes}")


def _load_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config must be a mapping: {path}")
    return raw


def _required_str(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty string")
    return value


def _token_list(item: dict[str, Any], key: str) -> tuple[str, ...]:
    values = item.get(key) or []
    if not isinstance(values, list):
        raise ConfigError(f"{key} must be a list")
    for value in values:
        if not isinstance(value, str):
            raise ConfigError(f"{key} entries must be strings")
        _validate_token(value, key)
    return tuple(values)


def _positive_number(item: dict[str, Any], key: str) -> float:
    value = item.get(key)
    if not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{key} must be positive")
    return float(value)


def _required_bool(item: dict[str, Any], key: str) -> bool:
    value = item.get(key)
    if type(value) is not bool:
        raise ConfigError(f"{key} must be a boolean")
    return value


def _positive_int(item: dict[str, Any], key: str) -> int:
    value = item.get(key)
    if not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{key} must be positive")
    return value


def _optional_positive_int(item: dict[str, Any], key: str) -> int | None:
    if key not in item or item.get(key) is None:
        return None
    value = item.get(key)
    if type(value) is not int or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{key} must be a positive integer")
    return value


def _schema(item: dict[str, Any], key: str, *, require_object: bool) -> dict[str, Any]:
    schema = item.get(key)
    if not isinstance(schema, dict):
        raise ConfigError(f"{key} must be a JSON schema object")
    if require_object and schema.get("type") != "object":
        raise ConfigError(f"{key} must be an object schema")
    try:
        Draft7Validator.check_schema(schema)
    except SchemaError as exc:
        raise ConfigError(f"{key} is not a valid JSON schema: {exc.message}") from exc
    return dict(schema)


def _validate_token(value: str, label: str) -> None:
    if not SAFE_TOKEN_RE.fullmatch(value):
        raise ConfigError(f"unsafe {label} token: {value}")


def _validate_subject(subject: str) -> None:
    parts = subject.split(".")
    if len(parts) < 2 or any(not SAFE_TOKEN_RE.fullmatch(part) for part in parts):
        raise ConfigError(f"unsafe NATS subject: {subject}")
