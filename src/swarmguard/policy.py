from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


class PolicyEngine:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.reload()

    def reload(self) -> None:
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self.agents: dict[str, Any] = raw.get("agents", {})

    def evaluate(self, agent_id: str, tool: str, arguments: dict[str, Any]) -> Decision:
        agent = self.agents.get(agent_id)
        if not agent:
            return Decision(False, f"unknown agent: {agent_id}")
        rule = agent.get("tools", {}).get(tool)
        if rule is None:
            return Decision(False, f"{tool} is not allowed for {agent_id}")
        if rule is True or rule == {}:
            return Decision(True, "tool allowed")
        if not isinstance(rule, dict):
            return Decision(False, "invalid policy rule")

        if tool == "workspace_read":
            return self._workspace_read(rule, arguments)
        if tool == "web_lookup":
            return self._web_lookup(rule, arguments)
        if tool == "safe_shell":
            return self._safe_shell(rule, arguments)
        return Decision(True, "tool and arguments allowed")

    @staticmethod
    def _workspace_read(rule: dict[str, Any], arguments: dict[str, Any]) -> Decision:
        requested = Path(str(arguments.get("path", ""))).expanduser().resolve()
        for root in rule.get("roots", []):
            allowed_root = Path(root).expanduser().resolve()
            if requested == allowed_root or allowed_root in requested.parents:
                return Decision(True, f"path is inside {allowed_root}")
        return Decision(False, f"path is outside allowed roots: {requested}")

    @staticmethod
    def _web_lookup(rule: dict[str, Any], arguments: dict[str, Any]) -> Decision:
        url = str(arguments.get("url", ""))
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return Decision(False, "only HTTPS destinations are allowed")
        try:
            port = parsed.port
        except ValueError as exc:
            return Decision(False, f"invalid destination: {exc}")
        if port not in (None, 443):
            return Decision(False, f"destination port not allowed: {port}")
        host = (parsed.hostname or "").lower()
        domains = [str(item).lower() for item in rule.get("domains", [])]
        if "*" in domains or any(host == domain or host.endswith(f".{domain}") for domain in domains):
            return Decision(True, f"destination allowed: {host}")
        return Decision(False, f"destination not allowed: {host or '<missing>'}")

    @staticmethod
    def _safe_shell(rule: dict[str, Any], arguments: dict[str, Any]) -> Decision:
        command = str(arguments.get("command", ""))
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return Decision(False, f"invalid command: {exc}")
        if not argv:
            return Decision(False, "empty command")
        allowed = set(rule.get("commands", []))
        if argv[0] not in allowed:
            return Decision(False, f"command not allowed: {argv[0]}")
        if any(token in command for token in (";", "&&", "||", "|", ">", "<", "`", "$(")):
            return Decision(False, "shell operators are not allowed")
        return Decision(True, f"command allowed: {argv[0]}")
