from __future__ import annotations

import argparse
import asyncio
import contextlib
import http.client
import ipaddress
import os
import re
import shlex
import socket
import ssl
import subprocess
import time
from dataclasses import dataclass
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import yaml
from nats.aio.msg import Msg
from pydantic import ValidationError

from . import research, security
from .bus import connect, publish_event, wait_for_shutdown
from .otel import start_tool_attempt_span
from .protocol import (
    Event,
    EventKind,
    ExecutionStatus,
    PolicyDecision,
    ToolCallIdentity,
    ToolRequest,
    ToolResponse,
    tool_call_identity,
    tool_cancel_wildcard_subject,
)
from .registry import ToolManifest, ToolRegistry, enforce_output_limit, load_tool_registry, validate_json


_CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"


# ---------------------------------------------------------------------------
# SSRF-safe web lookup
# ---------------------------------------------------------------------------


class SsrfError(ValueError):
    """Raised when a web_lookup destination violates the SSRF policy."""


# Well-known cloud metadata endpoints that must never be reachable. The IPv4
# metadata address is link-local (already rejected), listed here for clarity and
# defense in depth; the IPv6 unique-local metadata address is also rejected.
_METADATA_ADDRESSES = {"169.254.169.254", "fd00:ec2::254"}

Resolver = Callable[[str], list[str]]
ConnectionFactory = Callable[[str, str], Any]


def _default_resolver(host: str) -> list[str]:
    addresses: list[str] = []
    for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP):
        addr = info[4][0]
        if addr not in addresses:
            addresses.append(addr)
    return addresses


def _is_public_address(addr: str) -> bool:
    if addr in _METADATA_ADDRESSES:
        return False
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return bool(ip.is_global)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that dials a pre-validated, pinned IP address.

    The TCP connection targets the pinned IP (so a later DNS answer cannot
    rebind us to an internal address), while TLS SNI/certificate verification
    and the HTTP ``Host`` header keep using the original hostname.
    """

    def __init__(self, host: str, pinned_ip: str, *, context: ssl.SSLContext, timeout: float):
        super().__init__(host, 443, context=context, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:  # pragma: no cover - exercised via real network only
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _default_connection_factory(host: str, pinned_ip: str) -> Any:  # pragma: no cover - network
    context = ssl.create_default_context()
    return _PinnedHTTPSConnection(host, pinned_ip, context=context, timeout=10)


def fetch_url(
    url: str,
    *,
    resolver: Resolver | None = None,
    connection_factory: ConnectionFactory | None = None,
) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise SsrfError("only https:// destinations are allowed")
    if parts.username or parts.password:
        raise SsrfError("URL userinfo (user:password@) is not allowed")
    host = parts.hostname
    if not host:
        raise SsrfError("destination host is missing")
    try:
        port = parts.port
    except ValueError as exc:
        raise SsrfError(f"invalid destination port: {exc}") from exc
    if port not in (None, 443):
        raise SsrfError(f"only port 443 is allowed, got: {port}")

    resolve = resolver or _default_resolver
    addresses = resolve(host)
    if not addresses:
        raise SsrfError(f"could not resolve host: {host}")
    for addr in addresses:
        if not _is_public_address(addr):
            raise SsrfError(f"destination resolves to a disallowed address: {addr}")

    pinned = addresses[0]
    factory = connection_factory or _default_connection_factory
    conn = factory(host, pinned)
    try:
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        conn.request("GET", path, headers={"User-Agent": "SwarmGuard/0.1", "Host": host})
        response = conn.getresponse()
        status = getattr(response, "status", 0)
        if 300 <= status < 400:
            raise SsrfError(f"redirects are disabled (received status {status})")
        body = response.read(20_000)
    finally:
        with contextlib.suppress(Exception):
            conn.close()
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return str(body)


# ---------------------------------------------------------------------------
# workspace_read worker-side containment (independent of the gateway)
# ---------------------------------------------------------------------------


class WorkspaceAccessError(PermissionError):
    """Raised when a workspace_read path escapes the allowed roots."""


def _policy_workspace_roots() -> list[Path]:
    path = Path(os.getenv("POLICY_FILE", str(_CONFIG_ROOT / "policies.yaml")))
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return []
    roots: list[Path] = []
    for agent in (raw.get("agents") or {}).values():
        rule = ((agent or {}).get("tools") or {}).get("workspace_read") or {}
        for root in rule.get("roots", []) or []:
            roots.append(Path(str(root)))
    return roots


def _allowed_workspace_roots() -> list[Path]:
    env = os.getenv("SWARMGUARD_WORKSPACE_ROOTS")
    raw_roots: list[Path] = []
    if env is not None:
        for part in re.split(r"[:,]", env):
            part = part.strip()
            if part:
                raw_roots.append(Path(part))
    else:
        raw_roots.extend(_policy_workspace_roots())
    resolved: list[Path] = []
    for root in raw_roots:
        try:
            resolved.append(Path(os.path.realpath(os.path.expanduser(str(root)))))
        except OSError:
            continue
    return resolved


def _resolve_within_roots(path_value: Any) -> Path:
    roots = _allowed_workspace_roots()
    if not roots:
        # Fail closed: without configured roots, no filesystem read is permitted.
        raise WorkspaceAccessError("no workspace roots are configured")
    resolved = Path(os.path.realpath(os.path.expanduser(str(path_value))))
    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved
    raise WorkspaceAccessError(f"path escapes allowed workspace roots: {resolved}")


def execute(tool: str, arguments: dict[str, Any], run_id: str | None = None) -> Any:
    if tool in research.TOOLS:
        # run_id comes from the gateway-authenticated request, never from model arguments.
        return research.TOOLS[tool](arguments, run_id)
    if tool == "workspace_read":
        safe_path = _resolve_within_roots(arguments["path"])
        return safe_path.read_text(encoding="utf-8")[:20_000]
    if tool == "web_lookup":
        return fetch_url(str(arguments["url"]))
    if tool == "safe_shell":
        argv = shlex.split(str(arguments["command"]))
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
        return {"exit_code": result.returncode, "stdout": result.stdout[:10_000], "stderr": result.stderr[:10_000]}
    if tool == "echo_metadata":
        message = str(arguments["message"])
        return {"echo": message, "length": len(message)}
    raise ValueError(f"unknown tool: {tool}")


# ---------------------------------------------------------------------------
# Worker sandbox (resource limits, clean environment, network capability)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sandbox:
    max_cpu_seconds: int | None = None
    max_memory_bytes: int | None = None
    max_file_size_bytes: int | None = None
    max_open_files: int | None = None
    network: bool = True
    clean_env: bool = True
    fail_closed: bool = False
    env_keys: tuple[str, ...] = ()


_SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "HOME", "TMPDIR")


def _safe_subprocess_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    return env


def _clean_child_environment(keep: tuple[str, ...] = ()) -> None:
    safe = _safe_subprocess_env()
    safe.update({key: os.environ[key] for key in keep if key in os.environ})
    os.environ.clear()
    os.environ.update(safe)


def _disable_child_network() -> None:
    def _blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("network access is disabled for this non-network tool")

    socket.socket = _blocked  # type: ignore[assignment]
    socket.create_connection = _blocked  # type: ignore[assignment]
    with contextlib.suppress(Exception):
        socket.socketpair = _blocked  # type: ignore[assignment]


def _apply_sandbox(sandbox: Sandbox | None) -> None:
    if sandbox is None:
        return
    errors: list[str] = []
    try:
        import resource
    except Exception:  # pragma: no cover - resource is POSIX-only
        resource = None  # type: ignore[assignment]
        errors.append("resource module unavailable")

    if resource is not None:
        limits = (
            ("RLIMIT_CPU", sandbox.max_cpu_seconds),
            ("RLIMIT_AS", sandbox.max_memory_bytes),
            ("RLIMIT_FSIZE", sandbox.max_file_size_bytes),
            ("RLIMIT_NOFILE", sandbox.max_open_files),
        )
        for name, value in limits:
            if value is None:
                continue
            res = getattr(resource, name, None)
            if res is None:
                continue
            try:
                _soft, hard = resource.getrlimit(res)
                new_hard = hard if hard == resource.RLIM_INFINITY else max(value, hard)
                resource.setrlimit(res, (value, new_hard))
            except (ValueError, OSError) as exc:
                errors.append(f"{name}: {exc}")

    if not sandbox.network:
        _disable_child_network()
    if sandbox.clean_env:
        _clean_child_environment(sandbox.env_keys)

    if errors and sandbox.fail_closed:
        raise RuntimeError("sandbox could not be established: " + "; ".join(errors))


def sandbox_for_manifest(manifest: ToolManifest, *, fail_closed: bool) -> Sandbox:
    limits = manifest.resource_limits
    return Sandbox(
        max_cpu_seconds=limits.max_cpu_seconds,
        max_memory_bytes=limits.max_memory_bytes,
        max_file_size_bytes=limits.max_file_size_bytes,
        max_open_files=limits.max_open_files,
        network=manifest.network,
        clean_env=True,
        fail_closed=fail_closed,
        env_keys=manifest.env,
    )


async def execute_async(
    tool: str,
    arguments: dict[str, Any],
    *,
    deadline_at: float | None = None,
    sandbox: Sandbox | None = None,
    run_id: str | None = None,
) -> Any:
    timeout = _remaining_deadline(deadline_at)
    if timeout is not None and timeout <= 0:
        raise asyncio.TimeoutError()
    if tool == "safe_shell":
        return await _execute_safe_shell_async(arguments, timeout, sandbox=sandbox)
    return await _execute_sync_in_child(tool, arguments, deadline_at=deadline_at, sandbox=sandbox, run_id=run_id)


async def _execute_sync_in_child(
    tool: str,
    arguments: dict[str, Any],
    *,
    deadline_at: float | None,
    sandbox: Sandbox | None = None,
    run_id: str | None = None,
) -> Any:
    context = get_context("fork")
    parent_conn, child_conn = context.Pipe(duplex=False)
    process = context.Process(target=_execute_child, args=(tool, arguments, child_conn, sandbox, run_id))
    process.start()
    child_conn.close()
    try:
        while True:
            if parent_conn.poll(0):
                status, payload = parent_conn.recv()
                process.join(timeout=1)
                if status == "ok":
                    return payload
                raise payload
            if process.exitcode is not None:
                process.join(timeout=1)
                raise RuntimeError(f"tool child exited without a result: {process.exitcode}")
            remaining = _remaining_deadline(deadline_at)
            if remaining is not None and remaining <= 0:
                raise asyncio.TimeoutError()
            await asyncio.sleep(min(0.005, remaining if remaining is not None else 0.005))
    except (asyncio.CancelledError, asyncio.TimeoutError):
        _terminate_child(process)
        raise
    finally:
        parent_conn.close()
        if process.exitcode is not None:
            process.join(timeout=0)


def _execute_child(
    tool: str, arguments: dict[str, Any], conn: Connection, sandbox: Sandbox | None = None, run_id: str | None = None
) -> None:
    try:
        _apply_sandbox(sandbox)
        conn.send(("ok", execute(tool, arguments, run_id)))
    except BaseException as exc:
        try:
            conn.send(("err", exc))
        except BaseException:
            conn.send(("err", RuntimeError(f"{type(exc).__name__}: {exc}")))
    finally:
        conn.close()


def _terminate_child(process: BaseProcess) -> None:
    if process.is_alive():
        process.terminate()
        process.join(timeout=1)
    if process.is_alive():
        process.kill()
        process.join(timeout=1)
    else:
        process.join(timeout=0)


def _sandbox_preexec(sandbox: Sandbox | None) -> Callable[[], None] | None:
    if sandbox is None:
        return None

    def _preexec() -> None:  # pragma: no cover - runs in forked subprocess
        _apply_sandbox(
            Sandbox(
                max_cpu_seconds=sandbox.max_cpu_seconds,
                max_memory_bytes=sandbox.max_memory_bytes,
                max_file_size_bytes=sandbox.max_file_size_bytes,
                max_open_files=sandbox.max_open_files,
                # Environment is scrubbed via the ``env`` argument; network for an
                # external command cannot be dropped without namespaces on a
                # dependency-free host, so only rlimits are applied here.
                network=True,
                clean_env=False,
                fail_closed=sandbox.fail_closed,
            )
        )

    return _preexec


async def _execute_safe_shell_async(
    arguments: dict[str, Any], timeout: float | None, *, sandbox: Sandbox | None = None
) -> Any:
    argv = shlex.split(str(arguments["command"]))
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_safe_subprocess_env() if (sandbox and sandbox.clean_env) else None,
        preexec_fn=_sandbox_preexec(sandbox),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout if timeout is not None else 10)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except asyncio.TimeoutError:
            process.kill()
            with contextlib.suppress(ProcessLookupError):
                await process.wait()
        raise
    return {
        "exit_code": process.returncode,
        "stdout": stdout.decode(errors="replace")[:10_000],
        "stderr": stderr.decode(errors="replace")[:10_000],
    }


def _remaining_deadline(deadline_at: float | None) -> float | None:
    if deadline_at is None:
        return None
    return max(0.0, deadline_at - time.time())


def _bounded_deadline(existing_deadline_at: float | None, timeout_seconds: float) -> float:
    manifest_deadline = time.time() + timeout_seconds
    if existing_deadline_at is None:
        return manifest_deadline
    return min(existing_deadline_at, manifest_deadline)


@dataclass
class ActiveCall:
    task: asyncio.Task[Any]
    heartbeat: asyncio.Task[None]
    request: ToolRequest


class ToolWorker:
    def __init__(
        self,
        tool: str,
        *,
        heartbeat_interval_seconds: float | None = None,
        tool_registry: ToolRegistry | None = None,
    ):
        self.tool = tool
        self.tool_registry = tool_registry or load_tool_registry()
        self.manifest = self.tool_registry.resolve_tool(tool)
        self.sandbox = sandbox_for_manifest(self.manifest, fail_closed=security.is_production())
        self.heartbeat_interval_seconds = (
            heartbeat_interval_seconds
            if heartbeat_interval_seconds is not None
            else float(os.getenv("TOOL_HEARTBEAT_INTERVAL_SECONDS", "5"))
        )
        self.nc = None
        self._active: dict[ToolCallIdentity, ActiveCall] = {}

    async def start(self) -> None:
        self.nc = await connect(f"tool-{self.tool}")
        await self.nc.subscribe(self.manifest.worker_subject, queue=f"tool-{self.tool}", cb=self.handle)
        # Routed tool scope: only cancellations addressed to this tool are delivered here.
        await self.nc.subscribe(tool_cancel_wildcard_subject(self.tool), cb=self.handle_cancel)

    async def handle_cancel(self, msg: Msg) -> None:
        # Malformed payloads must do nothing rather than raise inside the subscription.
        try:
            request = ToolRequest.model_validate_json(msg.data)
        except (ValueError, ValidationError):
            return
        # Defense in depth: even if a foreign-tool cancel is routed here, ignore it.
        if request.tool != self.tool:
            return
        await self.cancel_tool_call(request)

    async def cancel_tool_call(self, request: ToolRequest) -> bool:
        """Cancel only the active call whose full canonical identity matches ``request``.

        Returns ``True`` when a matching in-flight call was cancelled. A colliding
        ``tool_call_id`` from a different run/agent/instance/tool/attempt (or a
        mismatched idempotency key) resolves to a different identity and is ignored.
        """
        identity = tool_call_identity(request)
        active = self._active.get(identity)
        if active is None:
            return False
        # The index key already encodes the identity; re-validate the stored request's
        # identity as a belt-and-suspenders payload check before cancelling.
        if tool_call_identity(active.request) != identity:
            return False
        active.heartbeat.cancel()
        active.task.cancel()
        return True

    async def handle(self, msg: Msg) -> None:
        assert self.nc is not None
        request = ToolRequest.model_validate_json(msg.data)
        with start_tool_attempt_span(
            request.run_id,
            request.tool_call_id or request.request_id,
            request.attempt,
            tool_name=request.tool,
            traceparent=request.traceparent,
        ):
            await self._handle_with_span(msg, request)

    async def _handle_with_span(self, msg: Msg, request: ToolRequest) -> None:
        assert self.nc is not None
        if request.tool != self.tool:
            response = ToolResponse(
                request_id=request.request_id,
                allowed=False,
                policy_decision=PolicyDecision.DENIED,
                execution_status=ExecutionStatus.DENIED,
                error=f"request tool {request.tool} does not match worker tool {self.tool}",
                reason=f"request tool {request.tool} does not match worker tool {self.tool}",
                **_response_envelope(request),
            )
            await msg.respond(response.model_dump_json().encode())
            return
        identity = tool_call_identity(request)
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
                payload={**base, "state": "started"},
                **_event_envelope(request),
            ),
        )
        heartbeat = asyncio.create_task(self._heartbeat(request, base))
        execution: asyncio.Task[Any] | None = None
        response: ToolResponse | None = None
        try:
            if self.heartbeat_interval_seconds == 0:
                await self._publish_heartbeat(request, base)
            validate_json(self.manifest.input_schema, request.arguments, label="input")
            execution = asyncio.create_task(
                execute_async(
                    self.tool,
                    request.arguments,
                    deadline_at=_bounded_deadline(request.deadline_at, self.manifest.timeout_seconds),
                    sandbox=self.sandbox,
                    run_id=request.run_id,
                )
            )
            self._active[identity] = ActiveCall(task=execution, heartbeat=heartbeat, request=request)
            result = await execution
            enforce_output_limit(result, self.manifest.resource_limits.max_output_bytes)
            validate_json(self.manifest.output_schema, result, label="output")
            response = ToolResponse(
                request_id=request.request_id,
                allowed=True,
                policy_decision=PolicyDecision.ALLOWED,
                execution_status=ExecutionStatus.COMPLETED,
                result=result,
                **_response_envelope(request),
            )
        except (asyncio.CancelledError, asyncio.TimeoutError):
            response = None
        except Exception as exc:
            response = ToolResponse(
                request_id=request.request_id,
                allowed=True,
                policy_decision=PolicyDecision.ALLOWED,
                execution_status=ExecutionStatus.FAILED,
                error=str(exc),
                **_response_envelope(request),
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            if execution is not None and not execution.done():
                execution.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await execution
            self._active.pop(identity, None)
        if response is not None:
            await msg.respond(response.model_dump_json().encode())

    async def _heartbeat(self, request: ToolRequest, base: dict[str, object]) -> None:
        if self.heartbeat_interval_seconds <= 0:
            return
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval_seconds)
                if _remaining_deadline(request.deadline_at) is not None and _remaining_deadline(request.deadline_at) <= 0:
                    await self.cancel_tool_call(request)
                    return
                await self._publish_heartbeat(request, base)
        except asyncio.CancelledError:
            return

    async def _publish_heartbeat(self, request: ToolRequest, base: dict[str, object]) -> None:
        assert self.nc is not None
        if _remaining_deadline(request.deadline_at) is not None and _remaining_deadline(request.deadline_at) <= 0:
            return
        await publish_event(
            self.nc,
            Event(
                run_id=request.run_id,
                kind=EventKind.TOOL_HEARTBEAT,
                payload={**base, "state": "heartbeat"},
                **_event_envelope(request),
            ),
        )


def _event_envelope(request: ToolRequest) -> dict[str, object | None]:
    return {
        "agent_id": request.agent_id,
        "agent_instance_id": request.agent_instance_id,
        "step_id": request.step_id,
        "tool_call_id": request.tool_call_id,
        "attempt": request.attempt,
        "sequence": request.sequence,
        "idempotency_key": request.idempotency_key,
        "traceparent": request.traceparent,
        "tracestate": request.tracestate,
        "trace_id": request.trace_id,
        "parent_id": request.parent_id,
    }


def _response_envelope(request: ToolRequest) -> dict[str, object | None]:
    return {
        "run_id": request.run_id,
        **_event_envelope(request),
    }


async def run(tool: str) -> None:
    worker = ToolWorker(tool)
    await worker.start()
    assert worker.nc is not None
    try:
        await wait_for_shutdown()
    finally:
        await worker.nc.drain()


def main() -> None:
    parser = argparse.ArgumentParser()
    registry = load_tool_registry()
    parser.add_argument("tool", choices=[name for name in registry.tool_names() if registry.resolve_tool(name).dispatchable])
    args = parser.parse_args()
    asyncio.run(run(args.tool))


if __name__ == "__main__":
    main()
