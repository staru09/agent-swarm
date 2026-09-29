from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bus import connect, publish_event
from .protocol import Event, EventKind


@dataclass(frozen=True)
class ProcessIdentity:
    agent_id: str
    pid: int


class ProcessRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def identities(self) -> list[ProcessIdentity]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [ProcessIdentity(**item) for item in raw.get("agents", [])]

    def resolve(self, host_pid: int | None, ancestors: list[int]) -> str | None:
        # ponytail: PID ancestry is enough for short demo runs; add process
        # start-time matching if the supervisor becomes long-lived.
        candidates = set(ancestors)
        if host_pid is not None:
            candidates.add(host_pid)
        for identity in self.identities():
            if identity.pid in candidates:
                return identity.agent_id
        return None


def _data_fields(raw: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for item in raw.get("data", raw.get("args", [])):
        name = item.get("name")
        if not name:
            continue
        if "value" in item:
            fields[name] = item["value"]
            continue
        for key in ("str", "int32", "int64", "u_int32", "u_int64", "bool", "sockaddr", "ipv4", "ipv6"):
            if key in item:
                fields[name] = item[key]
                break
        if "str_array" in item:
            fields[name] = item["str_array"].get("value", [])
    return fields


def normalize_tracee(raw: dict[str, Any], registry: ProcessRegistry, run_id: str) -> Event | None:
    """Normalize Tracee evidence with identity-only links to known agents.

    Tracee is independent OS evidence. It links by run_id/agent_id when process
    ancestry proves that identity, but it does not fabricate trace context.
    """
    name = raw.get("name") or raw.get("eventName")
    allowed = {
        "sched_process_exec",
        "sched_process_fork",
        "sched_process_exit",
        "security_file_open",
        "security_socket_connect",
    }
    if name not in allowed:
        return None

    process = raw.get("workload", {}).get("process", {})
    host_pid = (
        process.get("pid", {}).get("value")
        or process.get("thread", {}).get("host_tid")
        or raw.get("hostProcessId")
    )
    ancestors = [
        item.get("host_pid") or item.get("pid")
        for item in process.get("ancestors", [])
        if item.get("host_pid") or item.get("pid")
    ]
    if raw.get("hostParentProcessId"):
        ancestors.append(raw["hostParentProcessId"])
    agent_id = registry.resolve(int(host_pid) if host_pid is not None else None, [int(pid) for pid in ancestors])
    if agent_id is None:
        return None

    fields = _data_fields(raw)
    payload = {
        "event": name,
        "host_pid": host_pid,
        "executable": process.get("executable", raw.get("executable", {})).get("path"),
        "fields": fields,
        "classification": "agent_direct_activity",
        "execution_identity": {
            "run_id": run_id,
            "agent_id": agent_id,
        },
    }
    alert = False
    if name == "security_file_open":
        path = str(fields.get("pathname", fields.get("path", "")))
        if path.startswith(("/etc/shadow", "/root/", "/home/ubuntu/.ssh/")):
            alert = True
        elif not path.startswith("/opt/swarmguard/demo-workspace/"):
            return None
    elif name == "security_socket_connect":
        alert = False
    elif name == "sched_process_exec":
        executable = str(payload["executable"] or fields.get("pathname", ""))
        alert = executable.endswith(("/bash", "/sh", "/curl", "/wget", "/nc"))

    return Event(
        run_id=run_id,
        agent_id=agent_id,
        kind=EventKind.KERNEL_ALERT if alert else EventKind.KERNEL_EVENT,
        payload=payload,
    )


async def consume(registry_path: str, run_id: str) -> None:
    registry = ProcessRegistry(registry_path)
    nc = await connect("tracee-collector")
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            try:
                event = normalize_tracee(json.loads(line), registry, run_id)
                if event:
                    await publish_event(nc, event)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                print(f"ignored malformed Tracee event: {exc}", file=sys.stderr)
    finally:
        await nc.drain()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default=os.getenv("PROCESS_REGISTRY", "/tmp/swarmguard-processes.json"))
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    asyncio.run(consume(args.registry, args.run_id))


if __name__ == "__main__":
    main()
