from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from nats.aio.msg import Msg

from .bus import connect, publish_event
from .protocol import Event, EventKind, ToolRequest, ToolResponse, tool_execute_subject


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_url(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "SwarmGuard/0.1"})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
        return response.read(20_000).decode("utf-8", errors="replace")


def execute(tool: str, arguments: dict[str, Any]) -> Any:
    if tool == "workspace_read":
        path = Path(str(arguments["path"]))
        return path.read_text(encoding="utf-8")[:20_000]
    if tool == "web_lookup":
        return fetch_url(str(arguments["url"]))
    if tool == "safe_shell":
        argv = shlex.split(str(arguments["command"]))
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
        return {"exit_code": result.returncode, "stdout": result.stdout[:10_000], "stderr": result.stderr[:10_000]}
    raise ValueError(f"unknown tool: {tool}")


class ToolWorker:
    def __init__(self, tool: str):
        self.tool = tool
        self.nc = None

    async def start(self) -> None:
        self.nc = await connect(f"tool-{self.tool}")
        await self.nc.subscribe(tool_execute_subject(self.tool), queue=f"tool-{self.tool}", cb=self.handle)

    async def handle(self, msg: Msg) -> None:
        assert self.nc is not None
        request = ToolRequest.model_validate_json(msg.data)
        base = {
            "request_id": request.request_id,
            "tool": self.tool,
            "worker_pid": os.getpid(),
        }
        await publish_event(
            self.nc,
            Event(
                run_id=request.run_id,
                kind=EventKind.TOOL_STARTED,
                trace_id=request.trace_id,
                payload=base,
            ),
        )
        try:
            result = await asyncio.to_thread(execute, self.tool, request.arguments)
            response = ToolResponse(request_id=request.request_id, allowed=True, result=result)
            kind = EventKind.TOOL_COMPLETED
            payload = {**base, "ok": True}
        except Exception as exc:
            response = ToolResponse(request_id=request.request_id, allowed=True, error=str(exc))
            kind = EventKind.TOOL_FAILED
            payload = {**base, "ok": False, "error": str(exc)}
        await publish_event(
            self.nc,
            Event(
                run_id=request.run_id,
                kind=kind,
                trace_id=request.trace_id,
                payload=payload,
            ),
        )
        await msg.respond(response.model_dump_json().encode())


async def run(tool: str) -> None:
    worker = ToolWorker(tool)
    await worker.start()
    assert worker.nc is not None
    try:
        await asyncio.Future()
    finally:
        await worker.nc.drain()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tool", choices=["web_lookup", "workspace_read", "safe_shell"])
    args = parser.parse_args()
    asyncio.run(run(args.tool))


if __name__ == "__main__":
    main()
