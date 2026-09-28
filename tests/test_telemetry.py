import json
from pathlib import Path

from swarmguard.protocol import EventKind
from swarmguard.telemetry import ProcessRegistry, normalize_tracee


def registry(tmp_path: Path) -> ProcessRegistry:
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps({"agents": [{"agent_id": "researcher", "pid": 4242}]}),
        encoding="utf-8",
    )
    return ProcessRegistry(path)


def tracee_event(name: str, pid: int, data: list[dict] | None = None, ancestors: list[dict] | None = None) -> dict:
    return {
        "name": name,
        "workload": {
            "process": {
                "pid": {"value": pid},
                "executable": {"path": "/usr/bin/python3"},
                "ancestors": ancestors or [],
            }
        },
        "data": data or [],
    }


def test_sensitive_file_becomes_alert(tmp_path: Path) -> None:
    raw = tracee_event(
        "security_file_open",
        4242,
        [{"name": "pathname", "str": "/etc/shadow"}],
    )
    event = normalize_tracee(raw, registry(tmp_path), "run-1")
    assert event is not None
    assert event.agent_id == "researcher"
    assert event.kind == EventKind.KERNEL_ALERT


def test_child_process_is_attributed_by_ancestry(tmp_path: Path) -> None:
    raw = tracee_event("sched_process_exec", 5000, ancestors=[{"host_pid": 4242}])
    event = normalize_tracee(raw, registry(tmp_path), "run-1")
    assert event is not None
    assert event.agent_id == "researcher"


def test_unrelated_host_noise_is_dropped(tmp_path: Path) -> None:
    assert normalize_tracee(tracee_event("security_socket_connect", 9999), registry(tmp_path), "run-1") is None
    assert normalize_tracee(tracee_event("random_event", 4242), registry(tmp_path), "run-1") is None


def test_irrelevant_python_file_opens_are_dropped(tmp_path: Path) -> None:
    raw = tracee_event(
        "security_file_open",
        4242,
        [{"name": "pathname", "str": "/usr/lib/python3.14/asyncio/base_events.py"}],
    )
    assert normalize_tracee(raw, registry(tmp_path), "run-1") is None


def test_network_connection_is_evidence_not_automatic_alert(tmp_path: Path) -> None:
    event = normalize_tracee(tracee_event("security_socket_connect", 4242), registry(tmp_path), "run-1")
    assert event is not None
    assert event.kind == EventKind.KERNEL_EVENT


def test_tracee_v024_flat_json_is_supported(tmp_path: Path) -> None:
    raw = {
        "eventName": "sched_process_exec",
        "hostProcessId": 5000,
        "hostParentProcessId": 4242,
        "executable": {"path": "/usr/bin/curl"},
        "args": [{"name": "pathname", "type": "string", "value": "/usr/bin/curl"}],
    }
    event = normalize_tracee(raw, registry(tmp_path), "run-1")
    assert event is not None
    assert event.agent_id == "researcher"
    assert event.kind == EventKind.KERNEL_ALERT
    assert event.payload["fields"]["pathname"] == "/usr/bin/curl"
