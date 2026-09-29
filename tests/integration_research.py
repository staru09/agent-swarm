"""Live NATS/Postgres probe for the research deployment (no model or Exa calls).

Requires the compose stack. Pass --start-gateway when no gateway is running:
    python tests/integration_research.py --start-gateway
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from contextlib import suppress
from uuid import uuid4

import nats

from swarmguard.protocol import ToolRequest, ToolResponse, agent_tool_subject
from swarmguard.registry import load_agent_registry


NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
AGENTS = ["web-researcher", "paper-reviewer", "summary-writer", "hypothesis-generator"]
DECOY_KINDS = {"tool.requested", "tool.denied", "security.decoy_triggered"}


async def expect_permission_violation(agent_id: str, subject: str) -> None:
    violation: asyncio.Future[Exception] = asyncio.get_running_loop().create_future()

    async def on_error(error: Exception) -> None:
        if not violation.done():
            violation.set_result(error)

    nc = await nats.connect(NATS_URL, user=agent_id, password=f"{agent_id}-dev", error_cb=on_error)
    await nc.publish(subject, ToolRequest(run_id="probe", tool="exa_paper_search").model_dump_json().encode())
    error = await asyncio.wait_for(violation, timeout=3)
    assert "permissions violation" in str(error).lower(), error
    await nc.close()


async def probe(run_id: str) -> dict[str, str]:
    registry = load_agent_registry()
    decoy_calls: dict[str, str] = {}
    for agent_id in AGENTS:
        nc = await nats.connect(NATS_URL, user=agent_id, password=f"{agent_id}-dev")
        for decoy in registry.resolve_agent(agent_id).decoy_tools:
            request = ToolRequest(run_id=run_id, agent_id=agent_id, tool=decoy, arguments={"query": "probe"})
            reply = await nc.request(agent_tool_subject(run_id, agent_id), request.model_dump_json().encode(), timeout=5)
            response = ToolResponse.model_validate_json(reply.data)
            assert response.error == "LoL you got scammed", response
            assert response.reason_code == "decoy_tool_invoked", response
            decoy_calls[request.tool_call_id or request.request_id] = f"{agent_id}:{decoy}"
        await nc.close()
    # Identity spoofing and direct worker bypass are rejected by NATS ACLs.
    await expect_permission_violation("web-researcher", agent_tool_subject(run_id, "paper-reviewer"))
    await expect_permission_violation("paper-reviewer", "private.tool.exa_paper_search.execute")
    return decoy_calls


async def check_postgres(run_id: str, decoy_calls: dict[str, str]) -> None:
    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        deadline = time.monotonic() + 20
        while True:
            rows = await conn.fetch("SELECT tool_call_id, kind FROM events WHERE run_id = $1", run_id)
            kinds: dict[str, set[str]] = {}
            for row in rows:
                kinds.setdefault(row["tool_call_id"], set()).add(row["kind"])
            if all(kinds.get(call) == DECOY_KINDS for call in decoy_calls) or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.5)
        for call, label in decoy_calls.items():
            assert kinds.get(call) == DECOY_KINDS, (label, kinds.get(call))
    finally:
        await conn.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-gateway", action="store_true")
    parser.add_argument("--run-id", default=f"probe-{uuid4().hex[:8]}")
    args = parser.parse_args()
    gateway = None
    if args.start_gateway:
        env = os.environ | {"NATS_URL": NATS_URL, "NATS_USER": "gateway", "NATS_PASSWORD": "gateway-dev"}
        gateway = subprocess.Popen([sys.executable, "-m", "swarmguard.gateway"], env=env)
        await asyncio.sleep(1.5)
    try:
        decoy_calls = await probe(args.run_id)
        if os.getenv("DATABASE_URL"):
            await check_postgres(args.run_id, decoy_calls)
        print(f"ok: {len(decoy_calls)} decoys denied with security events, ACL spoof/bypass rejected (run {args.run_id})")
    finally:
        if gateway is not None:
            gateway.terminate()
            with suppress(subprocess.TimeoutExpired):
                gateway.wait(timeout=5)


if __name__ == "__main__":
    asyncio.run(main())
