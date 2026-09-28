from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class EventKind(StrEnum):
    A2A_SENT = "a2a.sent"
    TOOL_REQUESTED = "tool.requested"
    TOOL_ALLOWED = "tool.allowed"
    TOOL_DENIED = "tool.denied"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    KERNEL_EVENT = "kernel.event"
    KERNEL_ALERT = "kernel.alert"


class Event(BaseModel):
    schema_version: str = "1"
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    agent_id: str | None = None
    kind: EventKind
    trace_id: str = Field(default_factory=lambda: str(uuid4()))
    parent_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolRequest(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    trace_id: str = Field(default_factory=lambda: str(uuid4()))
    parent_id: str | None = None
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResponse(BaseModel):
    request_id: str
    allowed: bool
    result: Any | None = None
    error: str | None = None
    reason: str | None = None


def agent_tool_subject(run_id: str, agent_id: str) -> str:
    return f"swarm.{run_id}.agent.{agent_id}.tool.request"


def agent_message_subject(run_id: str, sender: str, recipient: str) -> str:
    return f"swarm.{run_id}.agent.{sender}.message.{recipient}"


def tool_execute_subject(tool: str) -> str:
    return f"private.tool.{tool}.execute"


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
