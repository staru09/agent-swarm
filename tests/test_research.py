from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from swarmguard import research
from swarmguard.agent import Agent, RepeatedDecoyInvocation
from swarmguard.gateway import DECOY_ERROR, DECOY_REASON_CODE, ExecutionCoordinator
from swarmguard.harness import decode_signed_harness_record
from swarmguard.policy import PolicyEngine
from swarmguard.protocol import EventKind, ExecutionStatus, ToolRequest, agent_tool_subject
from swarmguard.registry import ConfigError, load_agent_registry, load_tool_registry, validate_json


CONFIG = Path(__file__).resolve().parents[1] / "config"
RESEARCH_AGENTS = ["web-researcher", "paper-reviewer", "summary-writer", "hypothesis-generator"]
DECOY_PAIRS = [
    (agent_id, decoy)
    for agent_id in RESEARCH_AGENTS
    for decoy in load_agent_registry().resolve_agent(agent_id).decoy_tools
]


class RecordingEvents:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.events.append(event)


class NoWorker:
    def __init__(self) -> None:
        self.requests: list[ToolRequest] = []

    async def request(self, tool: str, request: ToolRequest, timeout: float) -> Any:
        self.requests.append(request)
        raise AssertionError(f"{tool} must never reach a worker")


def coordinator(worker: NoWorker, events: RecordingEvents) -> ExecutionCoordinator:
    return ExecutionCoordinator(
        policy=PolicyEngine(CONFIG / "policies.yaml"),
        worker_client=worker,
        event_publisher=events.publish,
    )


@pytest.fixture(autouse=True)
def artifacts_root(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(research, "ARTIFACTS_ROOT", tmp_path)
    return tmp_path


def test_every_research_agent_sees_three_decoys() -> None:
    assert len(DECOY_PAIRS) == 12
    for agent_id in RESEARCH_AGENTS:
        registration = load_agent_registry().resolve_agent(agent_id)
        agent = Agent(agent_id, "run-1", client=object())
        assert [tool["name"] for tool in agent.tools] == sorted(registration.tools + registration.decoy_tools)


@pytest.mark.asyncio
@pytest.mark.parametrize(("agent_id", "decoy"), DECOY_PAIRS)
async def test_decoy_invocation_is_denied_with_security_event_and_no_worker(agent_id: str, decoy: str) -> None:
    events, worker = RecordingEvents(), NoWorker()
    request = ToolRequest(run_id="run-1", tool=decoy, arguments={"query": "anything"})

    response = await coordinator(worker, events).handle_tool_request(agent_tool_subject("run-1", agent_id), request)

    assert response.allowed is False
    assert response.execution_status == ExecutionStatus.DENIED
    assert response.error == DECOY_ERROR == "LoL you got scammed"
    assert response.reason_code == DECOY_REASON_CODE
    assert [event.kind for event in events.events] == [
        EventKind.TOOL_REQUESTED,
        EventKind.TOOL_DENIED,
        EventKind.SECURITY_DECOY_TRIGGERED,
    ]
    assert {(event.run_id, event.agent_id, event.tool_call_id, event.trace_id) for event in events.events} == {
        ("run-1", agent_id, request.tool_call_id, request.trace_id)
    }
    assert events.events[-1].payload["reason_code"] == DECOY_REASON_CODE
    assert worker.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_id", "tool", "arguments"),
    [
        ("web-researcher", "web_lookup", {"url": "https://arxiv.org"}),
        ("web-researcher", "safe_shell", {"command": "uname -a"}),
        ("web-researcher", "workspace_read", {"path": "/etc/passwd"}),
        ("web-researcher", "candidate_read", {}),
        ("paper-reviewer", "literature_summary_write", {"paragraph": "x"}),
        ("summary-writer", "candidate_read", {}),
        ("summary-writer", "reviewed_papers_write", {"reviews": []}),
        ("hypothesis-generator", "candidate_read", {}),
        ("hypothesis-generator", "reviewed_papers_read", {}),
    ],
)
async def test_research_roles_cannot_escalate_outside_their_allowlist(agent_id: str, tool: str, arguments: dict) -> None:
    events, worker = RecordingEvents(), NoWorker()

    response = await coordinator(worker, events).handle_tool_request(
        agent_tool_subject("run-1", agent_id), ToolRequest(run_id="run-1", tool=tool, arguments=arguments)
    )

    assert response.execution_status == ExecutionStatus.DENIED
    assert response.reason_code is None
    assert worker.requests == []
    assert EventKind.SECURITY_DECOY_TRIGGERED not in [event.kind for event in events.events]


def test_decoy_manifest_must_not_define_a_worker(tmp_path: Path) -> None:
    path = tmp_path / "tools.yaml"
    path.write_text(
        "tools:\n  - {version: 1, name: bait, description: d, input_schema: {type: object}, output_schema: {type: object},"
        " sensitivity: low, timeout_seconds: 1, retry_policy: {max_attempts: 1}, resource_limits: {max_output_bytes: 10},"
        " classification: decoy, dispatchable: false, worker: {subject: private.tool.bait.execute, capability: x}}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="decoy tools must not define a worker"):
        load_tool_registry(path)


# ---------------------------------------------------------------------------
# Agent harness: one decoy hit is survivable, a second one fails the stage
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


class GatewayBackedNats:
    """Routes the agent's NATS request through a real coordinator in-process."""

    def __init__(self, gateway: ExecutionCoordinator):
        self.gateway = gateway

    async def request(self, subject: str, data: bytes, timeout: int) -> SimpleNamespace:
        response = await self.gateway.handle_tool_request(subject, ToolRequest.model_validate_json(data))
        return SimpleNamespace(data=response.model_dump_json().encode())

    async def publish(self, subject: str, data: bytes) -> None:
        return None


class ScriptedModel:
    def __init__(self, *responses: Any):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_repeated_decoy_invocation_fails_the_agent_session() -> None:
    model = ScriptedModel(
        SimpleNamespace(content=[ToolUseBlock("toolu_1", "crossref_search", {"query": "a"})], stop_reason="tool_use"),
        SimpleNamespace(content=[ToolUseBlock("toolu_2", "general_web_fetch", {"url": "b"})], stop_reason="tool_use"),
    )
    agent = Agent("web-researcher", "run-1", client=SimpleNamespace(messages=model))
    agent.nc = GatewayBackedNats(coordinator(NoWorker(), RecordingEvents()))

    with pytest.raises(RepeatedDecoyInvocation):
        await agent.run_task("find papers")

    first_result = model.calls[1]["messages"][-1]["content"][0]
    assert first_result["is_error"] is True
    assert DECOY_ERROR in first_result["content"]


# ---------------------------------------------------------------------------
# Exa discovery and artifact tools (synthetic fixture, no API credits)
# ---------------------------------------------------------------------------


def exa_result(index: int, **overrides: Any) -> dict[str, Any]:
    props = {
        "title": f"Paper {index} on circuits",
        "year": 2024,
        "authors": [{"name": f"Author {index}"}],
        "abstract": f"Abstract {index} about mechanistic interpretability.",
        "doi": None,
    }
    props.update(overrides.pop("props", {}))
    result = {"url": f"https://example.org/paper/{index}", "title": props["title"], "entities": [{"type": "publication", "properties": props}]}
    result.update(overrides)
    return result


def exa_payload(count: int, extra: list[dict[str, Any]] | None = None) -> bytes:
    return json.dumps({"results": [exa_result(i) for i in range(count)] + (extra or [])}).encode()


@pytest.fixture
def fake_exa(monkeypatch) -> SimpleNamespace:
    exa = SimpleNamespace(calls=[], payload=exa_payload(12))

    def request(query: str, num_results: int, api_key: str) -> bytes:
        exa.calls.append((query, num_results))
        return exa.payload

    monkeypatch.setenv("EXA_API_KEY", "test-key")
    monkeypatch.setattr(research, "_exa_request", request)
    return exa


def test_exa_normalization_deduplicates_by_doi_url_and_title() -> None:
    duplicates = [
        exa_result(20, props={"doi": "10.1/ABC"}),
        exa_result(21, props={"doi": "https://doi.org/10.1/abc"}),  # same DOI
        exa_result(22, url="https://EXAMPLE.org/paper/0/"),  # same URL as paper 0
        exa_result(23, props={"title": "PAPER 1 on Circuits!"}),  # same title as paper 1
        exa_result(24, props={"abstract": ""}),  # unusable: no abstract
        exa_result(25, url="http://insecure.example/paper"),  # unusable: not https
    ]
    candidates = research.normalize_exa_results(json.loads(exa_payload(3, duplicates)), "2026-01-01T00:00:00+00:00")

    assert [paper["title"] for paper in candidates] == ["Paper 0 on circuits", "Paper 1 on circuits", "Paper 2 on circuits", "Paper 20 on circuits"]
    assert candidates[3]["doi"] == "10.1/abc"
    assert all(paper["paper_id"].startswith("p-") and len(paper["paper_id"]) == 12 for paper in candidates)


def test_exa_search_input_schema_enforces_10_to_15_results() -> None:
    schema = load_tool_registry().resolve_tool("exa_paper_search").input_schema
    validate_json(schema, {"query": "sparse autoencoders", "num_results": 10}, label="input")
    for bad in (9, 16):
        with pytest.raises(ConfigError):
            validate_json(schema, {"query": "sparse autoencoders", "num_results": bad}, label="input")


def test_exa_search_stores_candidates_once_and_replays_without_a_second_call(fake_exa, artifacts_root: Path) -> None:
    first = research.exa_paper_search({"query": "circuits", "num_results": 12}, "run-1")
    second = research.exa_paper_search({"query": "other query", "num_results": 15}, "run-1")

    assert first["candidate_count"] == 12 and first["replayed"] is False
    assert second["replayed"] is True and second["candidates"] == first["candidates"]
    assert fake_exa.calls == [("circuits", 12)]
    stored = json.loads((artifacts_root / "run-1" / research.CANDIDATES).read_text())
    assert stored["raw_response_sha256"] == research.sha256(exa_payload(12))
    assert len(research.load_candidates("run-1")) == 12


def test_exa_search_with_fewer_than_10_usable_results_is_incomplete(fake_exa, artifacts_root: Path) -> None:
    fake_exa.payload = exa_payload(9)

    with pytest.raises(research.ArtifactError, match="incomplete"):
        research.exa_paper_search({"query": "circuits", "num_results": 10}, "run-1")

    assert not (artifacts_root / "run-1" / research.CANDIDATES).exists()


def test_artifact_tools_reject_unsafe_run_ids() -> None:
    for run_id in (None, "", "../escape", "a/b"):
        with pytest.raises(research.ArtifactError, match="unsafe run_id"):
            research.candidate_read({}, run_id)


def reviews_for(candidates: list[dict[str, Any]], includes: int) -> list[dict[str, str]]:
    return [
        {"paper_id": paper["paper_id"], "decision": "include" if i < includes else "discard", "reason": f"reason {i}"}
        for i, paper in enumerate(candidates)
    ]


def test_research_artifact_chain_enforces_review_summary_and_hypothesis_rules(fake_exa, artifacts_root: Path) -> None:
    research.exa_paper_search({"query": "circuits", "num_results": 12}, "run-1")
    candidates = research.candidate_read({}, "run-1")["candidates"]
    ids = [paper["paper_id"] for paper in candidates]

    with pytest.raises(research.ArtifactError, match="at most|between 1 and 10"):
        research.reviewed_papers_write({"reviews": reviews_for(candidates, 11)}, "run-1")
    with pytest.raises(research.ArtifactError, match="missing"):
        research.reviewed_papers_write({"reviews": reviews_for(candidates, 3)[:-1]}, "run-1")
    reviews = reviews_for(candidates, 3)
    research.reviewed_papers_write({"reviews": reviews}, "run-1")
    research.reviewed_papers_write({"reviews": reviews}, "run-1")  # identical replay is idempotent
    with pytest.raises(research.ArtifactError, match="immutable"):
        research.reviewed_papers_write({"reviews": reviews_for(candidates, 2)}, "run-1")
    reviewed = research.reviewed_papers_read({}, "run-1")
    assert all(paper["url"] in reviewed for paper in candidates[:3])
    assert "## Discarded" in reviewed

    cited = f"Circuits recur [{ids[0]}] and generalize [{ids[1]}]."
    with pytest.raises(research.ArtifactError, match="one plain paragraph"):
        research.literature_summary_write({"paragraph": cited + "\n\nSecond paragraph."}, "run-1")
    with pytest.raises(research.ArtifactError, match="not included"):
        research.literature_summary_write({"paragraph": cited + f" Discarded [{ids[5]}]."}, "run-1")
    research.literature_summary_write({"paragraph": cited}, "run-1")
    assert research.literature_summary_read({}, "run-1") == cited + "\n"

    hypothesis = {"hypothesis": "Circuits transfer", "rationale": "Because they recur.", "evidence": [ids[0]]}
    with pytest.raises(research.ArtifactError, match="absent from the summary"):
        research.future_directions_write({"hypotheses": [dict(hypothesis, evidence=[ids[2]])]}, "run-1")
    research.future_directions_write({"hypotheses": [hypothesis] * 3}, "run-1")
    research.check_directions("run-1")


# ---------------------------------------------------------------------------
# Orchestrator handoff gate
# ---------------------------------------------------------------------------


class FakeNats:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.published.append((subject, data))

    async def drain(self) -> None:
        return None

    def records(self) -> list[Any]:
        return [decode_signed_harness_record(data) for _, data in self.published]


class RecordingOrchestrator(research.ResearchOrchestrator):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.launched: list[str] = []

    async def run_agent(self, stage: research.Stage) -> int:
        self.launched.append(stage.agent_id)
        return 0  # exits cleanly but produces no artifact


@pytest.mark.asyncio
async def test_orchestrator_stops_downstream_when_a_stage_artifact_is_missing(monkeypatch) -> None:
    nc = FakeNats()

    async def fake_connect(_name: str) -> FakeNats:
        return nc

    monkeypatch.setattr(research, "connect", fake_connect)
    orchestrator = RecordingOrchestrator("run-1", "mech interp")

    report = await orchestrator.run()

    assert report["status"] == "failed"
    assert orchestrator.launched == ["web-researcher"]
    assert [(record.kind, record.payload.get("status")) for record in nc.records()] == [
        ("run.started", None),
        ("run.ended", "failed"),
    ]


@pytest.mark.asyncio
async def test_orchestrator_resumes_validated_artifacts_without_relaunching_agents(fake_exa, monkeypatch) -> None:
    research.exa_paper_search({"query": "circuits", "num_results": 12}, "run-1")
    candidates = research.candidate_read({}, "run-1")["candidates"]
    research.reviewed_papers_write({"reviews": reviews_for(candidates, 2)}, "run-1")
    research.literature_summary_write({"paragraph": f"Only [{candidates[0]['paper_id']}] matters."}, "run-1")
    hypothesis = {"hypothesis": "It generalizes", "rationale": "Shown once.", "evidence": [candidates[0]["paper_id"]]}
    research.future_directions_write({"hypotheses": [hypothesis] * 3}, "run-1")
    nc = FakeNats()

    async def fake_connect(_name: str) -> FakeNats:
        return nc

    monkeypatch.setattr(research, "connect", fake_connect)
    orchestrator = RecordingOrchestrator("run-1", "mech interp")

    report = await orchestrator.run()

    assert report["status"] == "completed"
    assert orchestrator.launched == []
    assert fake_exa.calls == [("circuits", 12)]
    records = nc.records()
    artifacts = [record.payload["artifact"] for record in records if record.kind == "artifact"]
    assert [artifact["producing_agent_id"] for artifact in artifacts] == RESEARCH_AGENTS
    assert artifacts[1]["source_artifact_ids"] == ["run-1:candidates.json"]
    assert all(artifact["traceparent"].split("-")[1] == orchestrator.trace_id for artifact in artifacts)
    assert all(artifact["prompt_version"] and artifact["digest"] for artifact in artifacts)
    assert [record.payload["recipient"] for record in records if record.kind == "handoff"] == RESEARCH_AGENTS[1:] + ["operator"]


@pytest.mark.skipif(os.getenv("SWARMGUARD_LIVE_EXA") != "1" or not os.getenv("EXA_API_KEY"), reason="opt-in live Exa test")
def test_live_exa_search_returns_10_to_15_candidates() -> None:
    result = research.exa_paper_search({"query": "sparse autoencoders mechanistic interpretability", "num_results": 12}, "live-exa")
    assert 10 <= result["candidate_count"] <= 15
