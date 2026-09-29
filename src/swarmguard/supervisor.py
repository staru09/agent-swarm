from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .registry import load_agent_registry


def write_registry(path: Path, processes: dict[str, subprocess.Popen]) -> None:
    data = {
        "updated_at_ns": time.time_ns(),
        "agents": [
            {"agent_id": agent_id, "pid": process.pid}
            for agent_id, process in processes.items()
        ],
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(path)


def agent_env(agent_id: str, credentials_dir: Path | None) -> dict[str, str]:
    env = os.environ.copy()
    if credentials_dir:
        env["NATS_CREDS"] = str(credentials_dir / f"{agent_id}.creds")
    elif os.getenv("NATS_DEV_PASSWORDS", "false").lower() == "true":
        env["NATS_USER"] = agent_id
        env["NATS_PASSWORD"] = f"{agent_id}-dev"
    return env


def run(run_id: str, registry: Path, credentials_dir: Path | None) -> int:
    processes: dict[str, subprocess.Popen] = {}
    for registration in load_agent_registry().enabled_agents():
        agent_id = registration.agent_id
        task = registration.default_task
        if not task:
            # Workflow agents (e.g. the research stages) are launched by their orchestrator.
            continue
        env = agent_env(agent_id, credentials_dir)
        command = [
            sys.executable,
            "-m",
            "swarmguard.agent",
            agent_id,
            "--run-id",
            run_id,
            "--task",
            task,
        ]
        processes[agent_id] = subprocess.Popen(command, env=env)
    write_registry(registry, processes)
    return max(process.wait() for process in processes.values())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--registry", default="/tmp/swarmguard-processes.json")
    parser.add_argument("--credentials-dir")
    args = parser.parse_args()
    raise SystemExit(
        run(
            args.run_id,
            Path(args.registry),
            Path(args.credentials_dir) if args.credentials_dir else None,
        )
    )


if __name__ == "__main__":
    main()
