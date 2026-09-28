from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from nats.aio.msg import Msg
from pydantic import ValidationError

from .bus import connect, publish_event
from .policy import PolicyEngine
from .protocol import (
    Event,
    EventKind,
    ToolRequest,
    ToolResponse,
    parse_agent_message_subject,
    parse_agent_tool_subject,
    tool_execute_subject,
)


class PolicyGateway:
    def __init__(self, policy: PolicyEngine):
        self.policy = policy
        self.nc = None

    async def start(self) -> None:
        self.nc = await connect("policy-gateway")
        await self.nc.subscribe("swarm.*.agent.*.tool.request", cb=self.handle)
        await self.nc.subscribe("swarm.*.agent.*.message.*", cb=self.handle_message)

    async def handle_message(self, msg: Msg) -> None:
        assert self.nc is not None
        try:
            run_id, sender, recipient = parse_agent_message_subject(msg.subject)
            body = json.loads(msg.data)
            if body.get("run_id") != run_id or body.get("sender") != sender or body.get("recipient") != recipient:
                return
            text = str(body.get("text", ""))[:2_000]
        except (ValueError, TypeError):
            return
        await publish_event(
            self.nc,
            Event(
                run_id=run_id,
                agent_id=sender,
                kind=EventKind.A2A_SENT,
                payload={"recipient": recipient, "text": text},
            ),
        )

    async def handle(self, msg: Msg) -> None:
        assert self.nc is not None
        try:
            run_id, agent_id = parse_agent_tool_subject(msg.subject)
            request = ToolRequest.model_validate_json(msg.data)
            if request.run_id != run_id:
                raise ValueError("run_id does not match authenticated subject namespace")
        except (ValueError, ValidationError) as exc:
            await msg.respond(
                ToolResponse(request_id="invalid", allowed=False, error=str(exc), reason="invalid request")
                .model_dump_json()
                .encode()
            )
            return

        await publish_event(
            self.nc,
            Event(
                run_id=run_id,
                agent_id=agent_id,
                kind=EventKind.TOOL_REQUESTED,
                trace_id=request.trace_id,
                parent_id=request.parent_id,
                payload={"request_id": request.request_id, "tool": request.tool, "arguments": request.arguments},
            ),
        )
        decision = self.policy.evaluate(agent_id, request.tool, request.arguments)
        kind = EventKind.TOOL_ALLOWED if decision.allowed else EventKind.TOOL_DENIED
        await publish_event(
            self.nc,
            Event(
                run_id=run_id,
                agent_id=agent_id,
                kind=kind,
                trace_id=request.trace_id,
                payload={
                    "request_id": request.request_id,
                    "tool": request.tool,
                    "reason": decision.reason,
                },
            ),
        )

        if not decision.allowed:
            await msg.respond(
                ToolResponse(
                    request_id=request.request_id,
                    allowed=False,
                    error="policy denied request",
                    reason=decision.reason,
                )
                .model_dump_json()
                .encode()
            )
            return

        if request.tool == "workspace_read":
            request.arguments["path"] = str(Path(str(request.arguments["path"])).resolve())

        try:
            worker_reply = await self.nc.request(
                tool_execute_subject(request.tool),
                request.model_dump_json().encode(),
                timeout=float(os.getenv("TOOL_TIMEOUT_SECONDS", "20")),
            )
            await msg.respond(worker_reply.data)
        except asyncio.TimeoutError:
            await msg.respond(
                ToolResponse(
                    request_id=request.request_id,
                    allowed=True,
                    error="tool worker timed out",
                    reason=decision.reason,
                )
                .model_dump_json()
                .encode()
            )


async def run(policy_path: str) -> None:
    gateway = PolicyGateway(PolicyEngine(policy_path))
    await gateway.start()
    assert gateway.nc is not None
    try:
        await asyncio.Future()
    finally:
        await gateway.nc.drain()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", default=os.getenv("POLICY_FILE", "config/policies.yaml"))
    args = parser.parse_args()
    asyncio.run(run(args.policy))


if __name__ == "__main__":
    main()
