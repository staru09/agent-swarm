import pytest

from swarmguard.protocol import (
    agent_message_subject,
    agent_tool_subject,
    parse_agent_message_subject,
    parse_agent_tool_subject,
    tool_execute_subject,
)


def test_subjects() -> None:
    subject = agent_tool_subject("run-1", "researcher")
    assert subject == "swarm.run-1.agent.researcher.tool.request"
    assert parse_agent_tool_subject(subject) == ("run-1", "researcher")
    message = agent_message_subject("run-1", "researcher", "analyst")
    assert message.endswith(".message.analyst")
    assert parse_agent_message_subject(message) == ("run-1", "researcher", "analyst")
    assert tool_execute_subject("web_lookup") == "private.tool.web_lookup.execute"


@pytest.mark.parametrize(
    "subject",
    ["swarm.run.agent.researcher.tool", "audit.run.tool.requested", "swarm.run.agent.tool.request"],
)
def test_invalid_tool_subjects(subject: str) -> None:
    with pytest.raises(ValueError):
        parse_agent_tool_subject(subject)
