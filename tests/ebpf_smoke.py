from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from contextlib import suppress
from pathlib import Path


def main() -> None:
    run_id = f"ebpf-smoke-{int(time.time())}"
    registry = Path("/tmp/swarmguard-ebpf-smoke-registry.json")
    probe = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess,time; time.sleep(6); subprocess.run(['/bin/echo','ebpf-smoke']); time.sleep(2)",
        ]
    )
    registry.write_text(
        json.dumps({"agents": [{"agent_id": "researcher", "pid": probe.pid}]}),
        encoding="utf-8",
    )

    tracee = subprocess.Popen(
        [
            "docker",
            "run",
            "--rm",
            "--name",
            "swarmguard-ebpf-smoke",
            "--pid=host",
            "--privileged",
            "-v",
            "/etc/os-release:/etc/os-release-host:ro",
            "aquasec/tracee@sha256:cfbbfee972e64a644f6b1bac74ee26998e6e12442697be4c797ae563553a2a5b",
            "--output",
            "json",
            "--events",
            "sched_process_exec,sched_process_fork,sched_process_exit",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    collector = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "swarmguard.telemetry",
            "--run-id",
            run_id,
            "--registry",
            str(registry),
        ],
        stdin=tracee.stdout,
        env=os.environ
        | {
            "NATS_URL": "nats://127.0.0.1:4222",
            "NATS_USER": "telemetry",
            "NATS_PASSWORD": "telemetry-dev",
        },
    )
    assert tracee.stdout is not None
    tracee.stdout.close()

    try:
        assert probe.wait(timeout=15) == 0
        time.sleep(2)
        with urllib.request.urlopen(f"http://127.0.0.1:8000/api/events?run_id={run_id}", timeout=5) as response:
            events = json.load(response)
        kernel_events = [event for event in events if event["kind"].startswith("kernel.")]
        assert kernel_events, f"no correlated kernel events found in {events}"
        assert any(event["agent_id"] == "researcher" for event in kernel_events)
        print(json.dumps({"run_id": run_id, "kernel_events": kernel_events}, indent=2))
    finally:
        for process in (collector, tracee, probe):
            with suppress(ProcessLookupError):
                process.terminate()
        subprocess.run(
            ["docker", "stop", "swarmguard-ebpf-smoke"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        registry.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
