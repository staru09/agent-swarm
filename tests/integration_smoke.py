from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import urllib.request
from contextlib import suppress

import nats

from swarmguard.protocol import ToolRequest, ToolResponse, agent_message_subject, agent_tool_subject


async def main() -> None:
    base_env = os.environ | {"NATS_URL": "nats://127.0.0.1:4222"}
    gateway = subprocess.Popen(
        [sys.executable, "-m", "swarmguard.gateway"],
        env=base_env | {"NATS_USER": "gateway", "NATS_PASSWORD": "gateway-dev"},
    )
    worker = subprocess.Popen(
        [sys.executable, "-m", "swarmguard.tools", "safe_shell"],
        env=base_env | {"NATS_USER": "tools", "NATS_PASSWORD": "tools-dev"},
    )
    try:
        await asyncio.sleep(1)
        nc = await nats.connect(
            "nats://127.0.0.1:4222",
            user="operator",
            password="operator-dev",
        )
        allowed_request = ToolRequest(run_id="smoke", tool="safe_shell", arguments={"command": "uname -s"})
        denied_request = ToolRequest(
            run_id="smoke",
            tool="safe_shell",
            arguments={"command": "curl https://example.com"},
        )
        allowed = ToolResponse.model_validate_json(
            (
                await nc.request(
                    agent_tool_subject("smoke", "operator"),
                    allowed_request.model_dump_json().encode(),
                    timeout=5,
                )
            ).data
        )
        denied = ToolResponse.model_validate_json(
            (
                await nc.request(
                    agent_tool_subject("smoke", "operator"),
                    denied_request.model_dump_json().encode(),
                    timeout=5,
                )
            ).data
        )
        await nc.drain()
        assert allowed.allowed and allowed.result["exit_code"] == 0
        assert allowed.result["stdout"].strip() == "Linux"
        assert not denied.allowed and "command not allowed" in (denied.reason or "")

        permission_error: asyncio.Future[Exception] = asyncio.get_running_loop().create_future()

        async def on_error(error: Exception) -> None:
            if not permission_error.done():
                permission_error.set_result(error)

        researcher = await nats.connect(
            "nats://127.0.0.1:4222",
            user="researcher",
            password="researcher-dev",
            error_cb=on_error,
        )
        message = {
            "run_id": "smoke",
            "sender": "researcher",
            "recipient": "analyst",
            "text": "Check the collected evidence.",
        }
        await researcher.publish(
            agent_message_subject("smoke", "researcher", "analyst"),
            json.dumps(message).encode(),
        )
        spoofed = ToolRequest(run_id="smoke", tool="safe_shell", arguments={"command": "uname"})
        await researcher.publish(
            agent_tool_subject("smoke", "operator"),
            spoofed.model_dump_json().encode(),
        )
        violation = await asyncio.wait_for(permission_error, timeout=2)
        assert "permissions violation" in str(violation).lower()
        await researcher.drain()

        await asyncio.sleep(1)
        with urllib.request.urlopen("http://127.0.0.1:8000/api/events?run_id=smoke", timeout=5) as response:
            events = json.load(response)
        kinds = {event["kind"] for event in events}
        assert {"tool.requested", "tool.allowed", "tool.completed", "tool.denied", "a2a.sent"} <= kinds
        print(json.dumps({"allowed": allowed.model_dump(), "denied": denied.model_dump(), "events": len(events)}, indent=2))
    finally:
        for process in (gateway, worker):
            process.terminate()
        for process in (gateway, worker):
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)


if __name__ == "__main__":
    asyncio.run(main())
