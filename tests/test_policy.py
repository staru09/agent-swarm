from pathlib import Path

from swarmguard.policy import PolicyEngine


def write_policy(path: Path, root: Path) -> None:
    path.write_text(
        f"""
agents:
  researcher:
    tools:
      web_lookup:
        domains: [huggingface.co]
  analyst:
    tools:
      workspace_read:
        roots: [{root.as_posix()}]
  operator:
    tools:
      safe_shell:
        commands: [uname, echo]
""",
        encoding="utf-8",
    )


def test_tool_and_argument_policy(tmp_path: Path) -> None:
    policy_file = tmp_path / "policy.yaml"
    allowed_root = tmp_path / "workspace"
    allowed_root.mkdir()
    write_policy(policy_file, allowed_root)
    policy = PolicyEngine(policy_file)

    assert policy.evaluate("researcher", "web_lookup", {"url": "https://huggingface.co/models"}).allowed
    assert not policy.evaluate("researcher", "web_lookup", {"url": "https://example.com"}).allowed
    assert not policy.evaluate("researcher", "web_lookup", {"url": "ftp://huggingface.co/private"}).allowed
    assert not policy.evaluate("researcher", "web_lookup", {"url": "https://huggingface.co:444/private"}).allowed
    assert policy.evaluate("analyst", "workspace_read", {"path": str(allowed_root / "brief.txt")}).allowed
    assert not policy.evaluate("analyst", "workspace_read", {"path": "/etc/shadow"}).allowed
    assert policy.evaluate("operator", "safe_shell", {"command": "uname -a"}).allowed
    assert not policy.evaluate("operator", "safe_shell", {"command": "curl https://example.com"}).allowed
    assert not policy.evaluate("operator", "safe_shell", {"command": "echo ok && id"}).allowed


def test_unknown_agent_and_tool_are_denied(tmp_path: Path) -> None:
    policy_file = tmp_path / "policy.yaml"
    write_policy(policy_file, tmp_path)
    policy = PolicyEngine(policy_file)

    assert not policy.evaluate("intruder", "safe_shell", {}).allowed
    assert not policy.evaluate("researcher", "safe_shell", {"command": "uname"}).allowed
