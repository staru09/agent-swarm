from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

from anthropic import AsyncAnthropic

from .bus import connect
from .protocol import ToolRequest, ToolResponse, agent_message_subject, agent_tool_subject


ROLES = {
    "researcher": "You are a web researcher. Prefer web_lookup. Do not pretend a tool succeeded.",
    "analyst": "You are a document analyst. Prefer workspace_read. Do not pretend a tool succeeded.",
    "operator": "You are an operations agent. Prefer safe_shell. Do not pretend a tool succeeded.",
}

TOOLS = [
    {
        "name": "web_lookup",
        "description": "Fetch a public web URL.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "workspace_read",
        "description": "Read a UTF-8 file from the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "safe_shell",
        "description": "Run a restricted non-shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]


class Agent:
    def __init__(self, agent_id: str, run_id: str):
        self.agent_id = agent_id
        self.run_id = run_id
        self.nc = None
        self.client = AsyncAnthropic()

    async def connect(self) -> None:
        self.nc = await connect(f"agent-{self.agent_id}")

    async def ask_tool(self, name: str, arguments: dict[str, Any], parent_id: str | None = None) -> ToolResponse:
        assert self.nc is not None
        request = ToolRequest(run_id=self.run_id, tool=name, arguments=arguments, parent_id=parent_id)
        reply = await self.nc.request(
            agent_tool_subject(self.run_id, self.agent_id),
            request.model_dump_json().encode(),
            timeout=30,
        )
        return ToolResponse.model_validate_json(reply.data)

    async def send_message(self, recipient: str, text: str) -> None:
        assert self.nc is not None
        body = {
            "run_id": self.run_id,
            "sender": self.agent_id,
            "recipient": recipient,
            "text": text,
        }
        await self.nc.publish(
            agent_message_subject(self.run_id, self.agent_id, recipient),
            json.dumps(body).encode(),
        )

    async def run_task(self, task: str) -> list[ToolResponse]:
        response = await self.client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5"),
            max_tokens=1024,
            system=ROLES[self.agent_id],
            tools=TOOLS,
            messages=[{"role": "user", "content": task}],
        )
        results: list[ToolResponse] = []
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                results.append(await self.ask_tool(block.name, dict(block.input), parent_id=block.id))
        return results


async def run(agent_id: str, run_id: str, task: str) -> None:
    agent = Agent(agent_id, run_id)
    await agent.connect()
    try:
        results = await agent.run_task(task)
        print(json.dumps([result.model_dump() for result in results], indent=2, default=str))
    finally:
        assert agent.nc is not None
        await agent.nc.drain()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("agent_id", choices=sorted(ROLES))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.agent_id, args.run_id, args.task))


if __name__ == "__main__":
    main()
