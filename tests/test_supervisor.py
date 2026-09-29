from __future__ import annotations

from pathlib import Path
from typing import Any

from swarmguard import supervisor


class FakeProcess:
    next_pid = 1000

    def __init__(self, command: list[str], env: dict[str, str]):
        self.command = command
        self.env = env
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1

    def wait(self) -> int:
        return 0


def test_supervisor_starts_enabled_agents_from_registry(monkeypatch, tmp_path: Path) -> None:
    started: list[FakeProcess] = []

    def popen(command: list[str], *, env: dict[str, str], **_kwargs: Any) -> FakeProcess:
        process = FakeProcess(command, env)
        started.append(process)
        return process

    monkeypatch.setattr(supervisor.subprocess, "Popen", popen)

    exit_code = supervisor.run("run-1", tmp_path / "processes.json", credentials_dir=None)

    assert exit_code == 0
    assert [process.command[3] for process in started] == ["analyst", "operator", "researcher"]
    assert all("--task" in process.command for process in started)
    assert "cartographer" not in [process.command[3] for process in started]
