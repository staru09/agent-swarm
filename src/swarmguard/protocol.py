from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, NamedTuple, Self
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
W3C_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
W3C_PARENT_ID_RE = re.compile(r"^[0-9a-f]{16}$")
ZERO_TRACE_ID = "0" * 32
ZERO_PARENT_ID = "0" * 16
SUPPORTED_SCHEMA_VERSIONS = {"1"}


def _new_event_id() -> str:
    return str(uuid4())


def _new_trace_id() -> str:
    return uuid4().hex


def _new_parent_id() -> str:
    return uuid4().hex[:16]


def _traceparent(trace_id: str | None, parent_id: str | None) -> str | None:
    if not trace_id or not parent_id:
        return None
    if trace_id == ZERO_TRACE_ID or parent_id == ZERO_PARENT_ID:
        return None
    if W3C_TRACE_ID_RE.fullmatch(trace_id) and W3C_PARENT_ID_RE.fullmatch(parent_id):
        return f"00-{trace_id}-{parent_id}-01"
    return None


def _validate_schema_version(schema_version: str) -> None:
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported schema_version: {schema_version}")


def _sync_trace_context(model: Any) -> None:
    traceparent = getattr(model, "traceparent", None)
    if traceparent:
        match = TRACEPARENT_RE.fullmatch(traceparent)
        if not match:
            raise ValueError("traceparent must be a W3C trace context header")
        trace_id, parent_id = match.group(1), match.group(2)
        if trace_id == ZERO_TRACE_ID or parent_id == ZERO_PARENT_ID:
            raise ValueError("traceparent must not contain all-zero trace_id or parent_id")
        explicit_trace_id = getattr(model, "trace_id", None)
        explicit_parent_id = getattr(model, "parent_id", None)
        if explicit_trace_id and W3C_TRACE_ID_RE.fullmatch(explicit_trace_id) and explicit_trace_id != trace_id:
            raise ValueError("traceparent does not match trace_id")
        if explicit_parent_id and W3C_PARENT_ID_RE.fullmatch(explicit_parent_id) and explicit_parent_id != parent_id:
            raise ValueError("traceparent does not match parent_id")
        model.trace_id = trace_id
        model.parent_id = parent_id
        return

    if getattr(model, "trace_id", None) is None:
        model.trace_id = _new_trace_id()
    if getattr(model, "parent_id", None) is None:
        model.parent_id = _new_parent_id()
    model.traceparent = _traceparent(model.trace_id, model.parent_id)


def _canonical_idempotency_key(model: Any) -> str:
    payload = {
        "run_id": getattr(model, "run_id", None),
        "agent_id": getattr(model, "agent_id", None),
        "agent_instance_id": getattr(model, "agent_instance_id", None),
        "step_id": getattr(model, "step_id", None),
        "tool_call_id": getattr(model, "tool_call_id", None),
        "tool": getattr(model, "tool", None),
        "arguments": getattr(model, "arguments", None),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class EventKind(StrEnum):
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_EXHAUSTED = "run.exhausted"
    AGENT_SESSION_STARTED = "agent_session.started"
    AGENT_SESSION_COMPLETED = "agent_session.completed"
    AGENT_SESSION_FAILED = "agent_session.failed"
    AGENT_SESSION_CANCELLED = "agent_session.cancelled"
    AGENT_SESSION_EXHAUSTED = "agent_session.exhausted"
    MODEL_STEP_STARTED = "model_step.started"
    MODEL_STEP_COMPLETED = "model_step.completed"
    MODEL_STEP_FAILED = "model_step.failed"
    MODEL_STEP_CANCELLED = "model_step.cancelled"
    MODEL_STEP_EXHAUSTED = "model_step.exhausted"
    HANDOFF = "handoff"
    A2A_SENT = "a2a.sent"
    TOOL_REQUESTED = "tool.requested"
    TOOL_POLICY_DECIDED = "tool.policy_decided"
    TOOL_ALLOWED = "tool.allowed"
    TOOL_DISPATCHED = "tool.dispatched"
    TOOL_DENIED = "tool.denied"
    TOOL_STARTED = "tool.started"
    TOOL_HEARTBEAT = "tool.heartbeat"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    TOOL_TIMED_OUT = "tool.timed_out"
    TOOL_CANCELLED = "tool.cancelled"
    TOOL_DEAD_LETTER = "tool.dead_letter"
    KERNEL_EVENT = "kernel.event"
    KERNEL_ALERT = "kernel.alert"
    SECURITY_DECOY_TRIGGERED = "security.decoy_triggered"
    ARTIFACT_CREATED = "artifact.created"


class PolicyDecision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class ToolCallState(StrEnum):
    REQUESTED = "requested"
    POLICY_DECIDED = "policy_decided"
    DISPATCHED = "dispatched"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


TOOL_CALL_TRANSITIONS: dict[ToolCallState, set[ToolCallState]] = {
    ToolCallState.REQUESTED: {
        ToolCallState.POLICY_DECIDED,
        ToolCallState.TIMED_OUT,
        ToolCallState.CANCELLED,
    },
    ToolCallState.POLICY_DECIDED: {
        ToolCallState.DISPATCHED,
        ToolCallState.DENIED,
        ToolCallState.TIMED_OUT,
        ToolCallState.CANCELLED,
    },
    ToolCallState.DISPATCHED: {
        ToolCallState.STARTED,
        ToolCallState.TIMED_OUT,
        ToolCallState.CANCELLED,
    },
    ToolCallState.STARTED: {
        ToolCallState.COMPLETED,
        ToolCallState.FAILED,
        ToolCallState.TIMED_OUT,
        ToolCallState.CANCELLED,
    },
    ToolCallState.COMPLETED: set(),
    ToolCallState.FAILED: set(),
    ToolCallState.DENIED: set(),
    ToolCallState.TIMED_OUT: set(),
    ToolCallState.CANCELLED: set(),
}


def validate_tool_call_transition(current: ToolCallState | str, next_state: ToolCallState | str) -> None:
    current_state = ToolCallState(current)
    target_state = ToolCallState(next_state)
    if target_state not in TOOL_CALL_TRANSITIONS[current_state]:
        raise ValueError(f"invalid tool-call transition: {current_state.value} -> {target_state.value}")


class Event(BaseModel):
    schema_version: str = "1"
    event_id: str = Field(default_factory=_new_event_id)
    run_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    agent_id: str | None = None
    agent_instance_id: str | None = None
    step_id: str | None = None
    tool_call_id: str | None = None
    attempt: int = Field(default=1, ge=1)
    sequence: int = Field(default=0, ge=0)
    idempotency_key: str | None = None
    traceparent: str | None = None
    tracestate: str | None = None
    kind: EventKind
    trace_id: str | None = None
    parent_id: str | None = None
    deadline_at: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def apply_envelope_defaults(self) -> Self:
        _validate_schema_version(self.schema_version)
        _sync_trace_context(self)
        if self.tool_call_id is None and "request_id" in self.payload:
            self.tool_call_id = str(self.payload["request_id"])
        if self.idempotency_key is None:
            self.idempotency_key = _canonical_idempotency_key(self)
        return self


class ToolRequest(BaseModel):
    schema_version: str = "1"
    event_id: str = Field(default_factory=_new_event_id)
    request_id: str = Field(default_factory=_new_event_id)
    run_id: str
    agent_id: str | None = None
    agent_instance_id: str | None = None
    step_id: str | None = None
    tool_call_id: str | None = None
    attempt: int = Field(default=1, ge=1)
    sequence: int = Field(default=0, ge=0)
    idempotency_key: str | None = None
    traceparent: str | None = None
    tracestate: str | None = None
    trace_id: str | None = None
    parent_id: str | None = None
    deadline_at: float | None = None
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def apply_envelope_defaults(self) -> Self:
        _validate_schema_version(self.schema_version)
        _sync_trace_context(self)
        if self.tool_call_id is None:
            self.tool_call_id = self.request_id
        if self.idempotency_key is None:
            self.idempotency_key = _canonical_idempotency_key(self)
        return self


class ToolResponse(BaseModel):
    schema_version: str = "1"
    event_id: str = Field(default_factory=_new_event_id)
    request_id: str
    run_id: str | None = None
    agent_id: str | None = None
    agent_instance_id: str | None = None
    step_id: str | None = None
    tool_call_id: str | None = None
    attempt: int = Field(default=1, ge=1)
    sequence: int = Field(default=0, ge=0)
    idempotency_key: str | None = None
    traceparent: str | None = None
    tracestate: str | None = None
    trace_id: str | None = None
    parent_id: str | None = None
    deadline_at: float | None = None
    allowed: bool
    policy_decision: PolicyDecision | None = None
    execution_status: ExecutionStatus | None = None
    result: Any | None = None
    error: str | None = None
    reason: str | None = None
    reason_code: str | None = None

    @classmethod
    def timed_out(cls, *, request_id: str, reason: str, **kwargs: Any) -> Self:
        return cls(
            request_id=request_id,
            allowed=True,
            policy_decision=PolicyDecision.ALLOWED,
            execution_status=ExecutionStatus.TIMED_OUT,
            error=reason,
            reason=reason,
            **kwargs,
        )

    @model_validator(mode="after")
    def apply_response_defaults(self) -> Self:
        _validate_schema_version(self.schema_version)
        _sync_trace_context(self)
        if self.tool_call_id is None:
            self.tool_call_id = self.request_id
        if self.policy_decision is None:
            self.policy_decision = PolicyDecision.ALLOWED if self.allowed else PolicyDecision.DENIED
        if self.execution_status is None:
            if self.policy_decision == PolicyDecision.DENIED or not self.allowed:
                self.execution_status = ExecutionStatus.DENIED
            elif self.error:
                self.execution_status = ExecutionStatus.FAILED
            else:
                self.execution_status = ExecutionStatus.COMPLETED
        expected_allowed = self.policy_decision == PolicyDecision.ALLOWED
        if self.allowed != expected_allowed:
            raise ValueError("policy_decision, allowed, and execution_status disagree")
        if self.policy_decision == PolicyDecision.DENIED and self.execution_status != ExecutionStatus.DENIED:
            raise ValueError("policy_decision, allowed, and execution_status disagree")
        if self.policy_decision == PolicyDecision.ALLOWED and self.execution_status == ExecutionStatus.DENIED:
            raise ValueError("policy_decision, allowed, and execution_status disagree")
        if self.idempotency_key is None:
            self.idempotency_key = _canonical_idempotency_key(self)
        return self


def agent_tool_subject(run_id: str, agent_id: str) -> str:
    return f"swarm.{run_id}.agent.{agent_id}.tool.request"


def agent_message_subject(run_id: str, sender: str, recipient: str) -> str:
    return f"swarm.{run_id}.agent.{sender}.message.{recipient}"


def agent_lifecycle_subject(run_id: str, agent_id: str) -> str:
    return f"swarm.{run_id}.agent.{agent_id}.lifecycle"


def tool_execute_subject(tool: str) -> str:
    return f"private.tool.{tool}.execute"


def tool_cancel_subject(tool: str, tool_call_id: str) -> str:
    return f"private.tool_call.{tool}.{tool_call_id}.cancel"


def tool_cancel_wildcard_subject(tool: str) -> str:
    return f"private.tool_call.{tool}.*.cancel"


class ToolCallIdentity(NamedTuple):
    """Canonical, fully scoped identity of a single tool-call attempt.

    Cancellation must target this whole identity, never ``tool_call_id`` alone:
    colliding ``tool_call_id`` values across runs/agents/instances/tools/attempts
    would otherwise cancel unrelated work.
    """

    run_id: str
    agent_id: str | None
    agent_instance_id: str | None
    tool: str
    tool_call_id: str
    attempt: int
    idempotency_key: str | None


def tool_call_identity(request: ToolRequest) -> ToolCallIdentity:
    return ToolCallIdentity(
        run_id=request.run_id,
        agent_id=request.agent_id,
        agent_instance_id=request.agent_instance_id,
        tool=request.tool,
        tool_call_id=request.tool_call_id or request.request_id,
        attempt=request.attempt,
        idempotency_key=request.idempotency_key,
    )


def event_subject(run_id: str, kind: EventKind) -> str:
    return f"audit.{run_id}.{kind.value}"


def parse_agent_tool_subject(subject: str) -> tuple[str, str]:
    tokens = subject.split(".")
    if (
        len(tokens) != 6
        or tokens[0] != "swarm"
        or tokens[2] != "agent"
        or tokens[4:] != ["tool", "request"]
    ):
        raise ValueError(f"invalid agent tool subject: {subject}")
    return tokens[1], tokens[3]


def parse_agent_message_subject(subject: str) -> tuple[str, str, str]:
    tokens = subject.split(".")
    if len(tokens) != 6 or tokens[0] != "swarm" or tokens[2] != "agent" or tokens[4] != "message":
        raise ValueError(f"invalid agent message subject: {subject}")
    return tokens[1], tokens[3], tokens[5]


def parse_agent_lifecycle_subject(subject: str) -> tuple[str, str]:
    tokens = subject.split(".")
    if len(tokens) != 5 or tokens[0] != "swarm" or tokens[2] != "agent" or tokens[4] != "lifecycle":
        raise ValueError(f"invalid agent lifecycle subject: {subject}")
    return tokens[1], tokens[3]
