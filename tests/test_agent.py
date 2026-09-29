from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from swarmguard.agent import Agent, MaxModelTurnsExceeded
from swarmguard.harness import InMemoryHarnessRecorder
from swarmguard.protocol import (
    EventKind,
    ToolRequest,
    ToolResponse,
    agent_lifecycle_subject,
    agent_message_subject,
    agent_tool_subject,
)


@dataclass(frozen=True)
class TextBlock:
    text: str
    type: str = "text"


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass(frozen=True)
class FakeResponse:
    content: list[Any]


class FakeMessages:
    def __init__(self, responses: list[FakeResponse | BaseException]):
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("model called more times than expected")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeAnthropicClient:
    def __init__(self, responses: list[FakeResponse | BaseException]):
        self.messages = FakeMessages(responses)


class FakeNatsGateway:
    def __init__(self) -> None:
        self.requests: list[ToolRequest] = []
        self.subjects: list[str] = []
        self.published: list[tuple[str, bytes]] = []

    async def request(self, subject: str, data: bytes, timeout: int) -> SimpleNamespace:
        self.subjects.append(subject)
        request = ToolRequest.model_validate_json(data)
        self.requests.append(request)
        response = ToolResponse(
            request_id=request.request_id,
            run_id=request.run_id,
            agent_instance_id=request.agent_instance_id,
            step_id=request.step_id,
            tool_call_id=request.tool_call_id,
            traceparent=request.traceparent,
            allowed=True,
            result={"echo": request.arguments, "tool": request.tool},
        )
        return SimpleNamespace(data=response.model_dump_json().encode())

    async def publish(self, subject: str, data: bytes) -> None:
        self.published.append((subject, data))


class FailingNatsGateway:
    async def request(self, subject: str, data: bytes, timeout: int) -> SimpleNamespace:
        raise RuntimeError("gateway unavailable")


def lifecycle_payloads(records: InMemoryHarnessRecorder, kind: str) -> list[dict[str, Any]]:
    return [record.payload for record in records.records if record.kind == kind]


@pytest.mark.asyncio
async def test_agent_returns_multiple_tool_results_to_following_model_turn() -> None:
    client = FakeAnthropicClient(
        [
            FakeResponse(
                [
                    TextBlock("I need two tools."),
                    ToolUseBlock("toolu-web", "web_lookup", {"url": "https://example.com"}),
                    ToolUseBlock("toolu-read", "workspace_read", {"path": "README.md"}),
                ]
            ),
            FakeResponse([TextBlock("Done.")]),
        ]
    )
    gateway = FakeNatsGateway()
    recorder = InMemoryHarnessRecorder()
    agent = Agent(
        "researcher",
        "run-1",
        client=client,
        harness=recorder,
        agent_instance_id="agent-instance-1",
        max_model_turns=3,
    )
    agent.nc = gateway

    results = await agent.run_task("research example.com")

    assert [response.tool_call_id for response in results] == ["toolu-web", "toolu-read"]
    assert [request.tool_call_id for request in gateway.requests] == ["toolu-web", "toolu-read"]
    assert gateway.subjects == [
        agent_tool_subject("run-1", "researcher"),
        agent_tool_subject("run-1", "researcher"),
    ]
    assert len(client.messages.calls) == 2
    follow_up_messages = client.messages.calls[1]["messages"]
    assert follow_up_messages[0] == {"role": "user", "content": "research example.com"}
    assert follow_up_messages[1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "I need two tools."},
            {
                "type": "tool_use",
                "id": "toolu-web",
                "name": "web_lookup",
                "input": {"url": "https://example.com"},
            },
            {
                "type": "tool_use",
                "id": "toolu-read",
                "name": "workspace_read",
                "input": {"path": "README.md"},
            },
        ],
    }
    assert follow_up_messages[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu-web",
                "content": json.dumps({"echo": {"url": "https://example.com"}, "tool": "web_lookup"}, sort_keys=True),
            },
            {
                "type": "tool_result",
                "tool_use_id": "toolu-read",
                "content": json.dumps({"echo": {"path": "README.md"}, "tool": "workspace_read"}, sort_keys=True),
            },
        ],
    }
    assert [record.kind for record in recorder.records] == [
        "run.started",
        "agent_session.started",
        "model_step.started",
        "tool.intent",
        "tool.intent",
        "model_step.ended",
        "model_step.started",
        "model_step.ended",
        "agent_session.ended",
        "run.ended",
    ]


@pytest.mark.asyncio
async def test_agent_stops_after_assistant_turn_without_tools() -> None:
    client = FakeAnthropicClient([FakeResponse([TextBlock("No tools needed.")])])
    gateway = FakeNatsGateway()
    agent = Agent("analyst", "run-1", client=client, agent_instance_id="agent-instance-1")
    agent.nc = gateway

    results = await agent.run_task("summarize from memory")

    assert results == []
    assert gateway.requests == []
    assert len(client.messages.calls) == 1


@pytest.mark.asyncio
async def test_agent_publishes_signed_lifecycle_records_to_owned_swarm_subject() -> None:
    client = FakeAnthropicClient([FakeResponse([TextBlock("No tools needed.")])])
    gateway = FakeNatsGateway()
    agent = Agent("analyst", "run-1", client=client, agent_instance_id="agent-instance-1")
    agent.nc = gateway

    await agent.run_task("summarize from memory")

    lifecycle = [
        (subject, data)
        for subject, data in gateway.published
        if subject == agent_lifecycle_subject("run-1", "analyst")
    ]
    assert len(lifecycle) == 6
    assert not any(subject.startswith("audit.") for subject, _ in gateway.published)
    payload = json.loads(lifecycle[-1][1])
    assert payload["signature"]
    assert payload["record"]["kind"] == "run.ended"
    assert payload["record"]["payload"]["status"] == "completed"


@pytest.mark.asyncio
async def test_agent_publishes_handoff_lifecycle_record_to_owned_swarm_subject() -> None:
    gateway = FakeNatsGateway()
    agent = Agent("researcher", "run-1", client=FakeAnthropicClient([]), agent_instance_id="agent-instance-1")
    agent.nc = gateway

    await agent.send_message("analyst", "please summarize")

    assert gateway.published[0][0] == agent_message_subject("run-1", "researcher", "analyst")
    lifecycle = [(subject, data) for subject, data in gateway.published if subject.endswith(".lifecycle")]
    assert lifecycle[0][0] == agent_lifecycle_subject("run-1", "researcher")
    payload = json.loads(lifecycle[0][1])
    assert payload["signature"]
    assert payload["record"]["kind"] == "handoff"
    assert payload["record"]["payload"] == {"recipient": "analyst", "message": "please summarize"}


@pytest.mark.asyncio
async def test_agent_marks_final_step_exhausted_when_tool_loop_reaches_max_model_turns() -> None:
    client = FakeAnthropicClient(
        [
            FakeResponse([ToolUseBlock("toolu-1", "safe_shell", {"command": "uname"})]),
            FakeResponse([ToolUseBlock("toolu-2", "safe_shell", {"command": "uname"})]),
        ]
    )
    gateway = FakeNatsGateway()
    recorder = InMemoryHarnessRecorder()
    agent = Agent(
        "operator",
        "run-1",
        client=client,
        harness=recorder,
        agent_instance_id="agent-instance-1",
        max_model_turns=2,
    )
    agent.nc = gateway

    with pytest.raises(MaxModelTurnsExceeded, match="reached max model turns: 2"):
        await agent.run_task("keep trying")

    assert [request.tool_call_id for request in gateway.requests] == ["toolu-1", "toolu-2"]
    assert lifecycle_payloads(recorder, "model_step.ended") == [
        {"status": "completed"},
        {"status": "exhausted", "error": "reached max model turns: 2"},
    ]
    assert recorder.records[-2].payload == {"status": "exhausted", "error": "reached max model turns: 2"}
    assert recorder.records[-1].payload == {"status": "exhausted", "error": "reached max model turns: 2"}


@pytest.mark.asyncio
async def test_agent_marks_started_step_failed_when_model_request_fails() -> None:
    client = FakeAnthropicClient([RuntimeError("model unavailable")])
    recorder = InMemoryHarnessRecorder()
    agent = Agent(
        "researcher",
        "run-1",
        client=client,
        harness=recorder,
        agent_instance_id="agent-instance-1",
    )
    agent.nc = FakeNatsGateway()

    with pytest.raises(RuntimeError, match="model unavailable"):
        await agent.run_task("lookup")

    assert lifecycle_payloads(recorder, "model_step.ended") == [
        {"status": "failed", "error": "model unavailable"}
    ]
    assert recorder.records[-2].payload == {"status": "failed", "error": "model unavailable"}
    assert recorder.records[-1].payload == {"status": "failed", "error": "model unavailable"}


@pytest.mark.asyncio
async def test_agent_marks_started_step_failed_when_tool_request_fails() -> None:
    client = FakeAnthropicClient(
        [FakeResponse([ToolUseBlock("toolu-1", "web_lookup", {"url": "https://example.com"})])]
    )
    recorder = InMemoryHarnessRecorder()
    agent = Agent(
        "researcher",
        "run-1",
        client=client,
        harness=recorder,
        agent_instance_id="agent-instance-1",
    )
    agent.nc = FailingNatsGateway()

    with pytest.raises(RuntimeError, match="gateway unavailable"):
        await agent.run_task("lookup")

    assert lifecycle_payloads(recorder, "model_step.ended") == [
        {"status": "failed", "error": "gateway unavailable"}
    ]
    assert recorder.records[-2].payload == {"status": "failed", "error": "gateway unavailable"}
    assert recorder.records[-1].payload == {"status": "failed", "error": "gateway unavailable"}


@pytest.mark.asyncio
async def test_agent_marks_started_step_cancelled_when_model_request_is_cancelled() -> None:
    client = FakeAnthropicClient([asyncio.CancelledError()])
    recorder = InMemoryHarnessRecorder()
    agent = Agent(
        "analyst",
        "run-1",
        client=client,
        harness=recorder,
        agent_instance_id="agent-instance-1",
    )
    agent.nc = FakeNatsGateway()

    with pytest.raises(asyncio.CancelledError):
        await agent.run_task("summarize")

    assert lifecycle_payloads(recorder, "model_step.ended") == [{"status": "cancelled"}]
    assert recorder.records[-2].payload == {"status": "cancelled"}
    assert recorder.records[-1].payload == {"status": "cancelled"}


@pytest.mark.asyncio
async def test_agent_populates_stable_correlation_identity_on_tool_requests() -> None:
    client = FakeAnthropicClient(
        [
            FakeResponse([ToolUseBlock("toolu-correlated", "web_lookup", {"url": "https://example.com"})]),
            FakeResponse([TextBlock("Done.")]),
        ]
    )
    gateway = FakeNatsGateway()
    agent = Agent(
        "researcher",
        "run-1",
        client=client,
        agent_instance_id="agent-instance-1",
        max_model_turns=3,
    )
    agent.nc = gateway

    await agent.run_task("lookup")

    request = gateway.requests[0]
    assert request.run_id == "run-1"
    assert request.agent_instance_id == "agent-instance-1"
    assert request.tool_call_id == "toolu-correlated"
    assert request.step_id == "agent-instance-1-turn-1"
    assert request.traceparent is not None
    assert request.traceparent.startswith("00-")
    assert request.trace_id is not None
    assert request.parent_id is not None
    assert len(request.trace_id) == 32
    assert len(request.parent_id) == 16


@pytest.mark.asyncio
async def test_agent_uses_registry_prompt_and_tool_manifests_for_extension_agent() -> None:
    client = FakeAnthropicClient([FakeResponse([TextBlock("No tools needed.")])])
    agent = Agent("cartographer", "run-1", client=client, agent_instance_id="agent-instance-1")
    agent.nc = FakeNatsGateway()

    await agent.run_task("describe metadata")

    call = client.messages.calls[0]
    assert call["system"] == "You are a map metadata assistant. Prefer echo_metadata."
    assert [tool["name"] for tool in call["tools"]] == [
        "echo_metadata",
        "safe_shell",
        "web_lookup",
        "workspace_read",
    ]
