from __future__ import annotations

import pytest

from swarmguard import tools
from swarmguard.tools import Sandbox, execute, execute_async, fetch_url


# ---------------------------------------------------------------------------
# SSRF-safe web lookup
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"<html>ok</html>") -> None:
        self.status = status
        self._body = body

    def read(self, amt: int | None = None) -> bytes:
        return self._body

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class RecordingConnection:
    instances: list["RecordingConnection"] = []

    def __init__(self, host: str, pinned_ip: str, response: FakeResponse | None = None) -> None:
        self.host = host
        self.pinned_ip = pinned_ip
        self.response = response or FakeResponse()
        self.requests: list[tuple[str, str, dict]] = []
        RecordingConnection.instances.append(self)

    def request(self, method: str, path: str, headers: dict | None = None) -> None:
        self.requests.append((method, path, headers or {}))

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        pass


def _factory(response: FakeResponse | None = None):
    def build(host: str, pinned_ip: str):
        return RecordingConnection(host, pinned_ip, response)

    return build


@pytest.fixture(autouse=True)
def _reset_connections():
    RecordingConnection.instances.clear()
    yield
    RecordingConnection.instances.clear()


def test_web_lookup_rejects_non_https() -> None:
    with pytest.raises(tools.SsrfError):
        fetch_url("http://example.com/", resolver=lambda host: ["93.184.216.34"], connection_factory=_factory())


def test_web_lookup_rejects_userinfo() -> None:
    with pytest.raises(tools.SsrfError):
        fetch_url(
            "https://user:pass@example.com/",
            resolver=lambda host: ["93.184.216.34"],
            connection_factory=_factory(),
        )


def test_web_lookup_rejects_non_443_port() -> None:
    with pytest.raises(tools.SsrfError):
        fetch_url(
            "https://example.com:8443/",
            resolver=lambda host: ["93.184.216.34"],
            connection_factory=_factory(),
        )


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",       # loopback
        "10.0.0.5",        # private
        "192.168.1.10",    # private
        "172.16.5.4",      # private
        "169.254.169.254", # link-local / cloud metadata
        "0.0.0.0",         # unspecified
        "224.0.0.1",       # multicast
        "::1",             # IPv6 loopback
        "fe80::1",         # IPv6 link-local
        "fd00:ec2::254",   # IPv6 unique-local / metadata
        "::",              # IPv6 unspecified
        "ff02::1",         # IPv6 multicast
    ],
)
def test_web_lookup_rejects_disallowed_resolved_addresses(addr: str) -> None:
    with pytest.raises(tools.SsrfError):
        fetch_url("https://malicious.example/", resolver=lambda host: [addr], connection_factory=_factory())


def test_web_lookup_rejects_when_any_answer_is_private() -> None:
    with pytest.raises(tools.SsrfError):
        fetch_url(
            "https://mixed.example/",
            resolver=lambda host: ["93.184.216.34", "127.0.0.1"],
            connection_factory=_factory(),
        )


def test_web_lookup_allows_public_ipv4_and_pins_address() -> None:
    body = fetch_url(
        "https://example.com/page",
        resolver=lambda host: ["93.184.216.34"],
        connection_factory=_factory(FakeResponse(200, b"PUBLIC-OK")),
    )
    assert body == "PUBLIC-OK"
    conn = RecordingConnection.instances[-1]
    # The connection is pinned to the validated IP, but keeps the hostname for
    # TLS verification and the Host header (DNS-rebinding resistance).
    assert conn.pinned_ip == "93.184.216.34"
    assert conn.host == "example.com"
    assert conn.requests[0][2]["Host"] == "example.com"


def test_web_lookup_allows_public_ipv6() -> None:
    body = fetch_url(
        "https://v6.example/",
        resolver=lambda host: ["2606:2800:220:1:248:1893:25c8:1946"],
        connection_factory=_factory(FakeResponse(200, b"V6-OK")),
    )
    assert body == "V6-OK"
    assert RecordingConnection.instances[-1].pinned_ip == "2606:2800:220:1:248:1893:25c8:1946"


def test_web_lookup_dns_rebinding_pins_validated_ip_not_rebound_ip() -> None:
    # Classic rebinding: first resolution is public and validated; the tool must
    # connect to the validated pinned IP, never re-resolving to a private one.
    resolutions = {"n": 0}

    def resolver(host: str) -> list[str]:
        resolutions["n"] += 1
        return ["93.184.216.34"]

    fetch_url(
        "https://rebind.example/",
        resolver=resolver,
        connection_factory=_factory(FakeResponse(200, b"OK")),
    )
    assert resolutions["n"] == 1
    assert RecordingConnection.instances[-1].pinned_ip == "93.184.216.34"


def test_web_lookup_disables_redirects() -> None:
    with pytest.raises(tools.SsrfError):
        fetch_url(
            "https://example.com/",
            resolver=lambda host: ["93.184.216.34"],
            connection_factory=_factory(FakeResponse(302, b"")),
        )


# ---------------------------------------------------------------------------
# workspace_read worker-side containment (independent of the gateway)
# ---------------------------------------------------------------------------


def test_workspace_read_allows_file_inside_configured_root(tmp_path, monkeypatch) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    target = root / "notes.txt"
    target.write_text("hello world", encoding="utf-8")
    monkeypatch.setenv("SWARMGUARD_WORKSPACE_ROOTS", str(root))

    assert execute("workspace_read", {"path": str(target)}) == "hello world"


def test_workspace_read_rejects_path_outside_root(tmp_path, monkeypatch) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("classified", encoding="utf-8")
    monkeypatch.setenv("SWARMGUARD_WORKSPACE_ROOTS", str(root))

    with pytest.raises(tools.WorkspaceAccessError):
        execute("workspace_read", {"path": str(outside)})


def test_workspace_read_rejects_symlink_escape(tmp_path, monkeypatch) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("classified", encoding="utf-8")
    link = root / "escape.txt"
    link.symlink_to(outside)
    monkeypatch.setenv("SWARMGUARD_WORKSPACE_ROOTS", str(root))

    with pytest.raises(tools.WorkspaceAccessError):
        execute("workspace_read", {"path": str(link)})


def test_workspace_read_fails_closed_without_configured_roots(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SWARMGUARD_WORKSPACE_ROOTS", "")
    monkeypatch.setenv("POLICY_FILE", str(tmp_path / "does-not-exist.yaml"))
    target = tmp_path / "x.txt"
    target.write_text("data", encoding="utf-8")
    with pytest.raises(tools.WorkspaceAccessError):
        execute("workspace_read", {"path": str(target)})


@pytest.mark.asyncio
async def test_direct_worker_bypass_is_denied_at_worker_boundary(tmp_path, monkeypatch) -> None:
    # Even if an attacker bypasses the gateway and speaks directly to the worker
    # child, path containment is enforced inside the worker execution.
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("classified", encoding="utf-8")
    monkeypatch.setenv("SWARMGUARD_WORKSPACE_ROOTS", str(root))
    # Keep the child fork lightweight/safe for the test environment.
    monkeypatch.setattr(tools, "_apply_sandbox", lambda sandbox: None)

    with pytest.raises(tools.WorkspaceAccessError):
        await execute_async("workspace_read", {"path": str(outside)}, sandbox=Sandbox(network=False))


# ---------------------------------------------------------------------------
# Worker sandbox: resource limits, clean environment, network capability
# ---------------------------------------------------------------------------


def _probe(_tool, _arguments, _run_id=None):
    import os as _os
    import resource as _res
    import socket as _sock

    network_ok = True
    try:
        _sock.socket()
    except OSError:
        network_ok = False
    return {
        "cpu": _res.getrlimit(_res.RLIMIT_CPU)[0],
        "fsize": _res.getrlimit(_res.RLIMIT_FSIZE)[0],
        "nofile": _res.getrlimit(_res.RLIMIT_NOFILE)[0],
        "as": _res.getrlimit(_res.RLIMIT_AS)[0],
        "network": network_ok,
        "env": sorted(_os.environ.keys()),
    }


@pytest.mark.asyncio
async def test_worker_child_applies_resource_limits_and_disables_network(monkeypatch) -> None:
    monkeypatch.setenv("SECRET_LEAK_CANARY", "do-not-inherit")
    monkeypatch.setattr(tools, "execute", _probe)

    sandbox = Sandbox(
        max_cpu_seconds=7,
        max_memory_bytes=4 * 1024 * 1024 * 1024,
        max_file_size_bytes=1_000_000,
        max_open_files=48,
        network=False,
        clean_env=True,
        fail_closed=False,
    )
    result = await execute_async("workspace_read", {"path": "ignored"}, sandbox=sandbox)

    assert result["cpu"] == 7
    assert result["fsize"] == 1_000_000
    assert result["nofile"] == 48
    assert result["as"] == 4 * 1024 * 1024 * 1024
    assert result["network"] is False
    assert "SECRET_LEAK_CANARY" not in result["env"]
    assert "PATH" in result["env"]


@pytest.mark.asyncio
async def test_network_tool_keeps_network_available(monkeypatch) -> None:
    monkeypatch.setattr(tools, "execute", _probe)
    sandbox = Sandbox(network=True, clean_env=True, max_open_files=48)
    result = await execute_async("web_lookup", {"url": "https://example.com"}, sandbox=sandbox)
    assert result["network"] is True


@pytest.mark.asyncio
async def test_sandbox_fails_closed_when_limit_cannot_be_applied(monkeypatch) -> None:
    monkeypatch.setattr(tools, "execute", _probe)
    # Requesting an open-files limit above the hard limit forces setrlimit to
    # fail; with fail_closed the worker child must refuse to run.
    sandbox = Sandbox(max_open_files=10 ** 12, network=False, clean_env=False, fail_closed=True)
    with pytest.raises(Exception):
        await execute_async("workspace_read", {"path": "ignored"}, sandbox=sandbox)
