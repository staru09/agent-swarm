from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from typing import Any
from uuid import uuid4

from anthropic import AsyncAnthropic

from .bus import connect
from .harness import HarnessAdapter, HarnessRecord, InMemoryHarnessRecorder, LifecycleStatus, encode_signed_harness_record
from .otel import start_model_step_span, start_run_span, start_tool_call_span
from .protocol import ToolRequest, ToolResponse, agent_lifecycle_subject, agent_message_subject, agent_tool_subject
from .registry import load_agent_registry, load_tool_registry


DECOY_REASON_CODE = "decoy_tool_invoked"


class MaxModelTurnsExceeded(RuntimeError):
    pass


class RepeatedDecoyInvocation(RuntimeError):
    pass


class AnthropicHarnessAdapter:
    def __init__(
        self,
        agent_id: str,
        run_id: str,
        *,
        client: Any | None = None,
        harness: HarnessAdapter | None = None,
        agent_instance_id: str | None = None,
        max_model_turns: int | None = None,
    ):
        self.agent_id = agent_id
        self.run_id = run_id
        self.nc = None
        self.client = client if client is not None else AsyncAnthropic()
        self.harness = harness if harness is not None else InMemoryHarnessRecorder()
        self.agent_instance_id = agent_instance_id or f"{agent_id}-{os.getpid()}"
        self.max_model_turns = max_model_turns or int(os.getenv("SWARMGUARD_MAX_MODEL_TURNS", "8"))
        # An orchestrator may pin every stage of a run to one trace.
        self.trace_id = os.getenv("SWARMGUARD_TRACE_ID") or uuid4().hex
        self.registration = load_agent_registry().resolve_agent(agent_id)
        self.tool_registry = load_tool_registry()
        catalog = self.registration.tools + self.registration.decoy_tools
        if os.getenv("SWARMGUARD_TOOL_CATALOG") == "all":
            # Experiment switch: show every registered tool; the gateway still enforces the role's policy.
            catalog = tuple(self.tool_registry.tool_names())
        self.tools = self.tool_registry.anthropic_tools(tuple(sorted(catalog)))

    async def connect(self) -> None:
        self.nc = await connect(f"agent-{self.agent_id}")

    async def ask_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        step_id: str,
        tool_call_id: str,
        traceparent: str,
    ) -> ToolResponse:
        assert self.nc is not None
        request = ToolRequest(
            run_id=self.run_id,
            agent_instance_id=self.agent_instance_id,
            step_id=step_id,
            tool_call_id=tool_call_id,
            traceparent=traceparent,
            tool=name,
            arguments=arguments,
        )
        reply = await self.nc.request(
            agent_tool_subject(self.run_id, self.agent_id),
            request.model_dump_json().encode(),
            timeout=30,
        )
        return ToolResponse.model_validate_json(reply.data)

    async def send_message(self, recipient: str, text: str) -> None:
        assert self.nc is not None
        body = {
            "run_id": self.run_id,
            "sender": self.agent_id,
            "recipient": recipient,
            "text": text,
        }
        record = self.harness.handoff(
            run_id=self.run_id,
            agent_id=self.agent_id,
            agent_instance_id=self.agent_instance_id,
            step_id=None,
            recipient=recipient,
            message=text,
        )
        await self.nc.publish(
            agent_message_subject(self.run_id, self.agent_id, recipient),
            json.dumps(body).encode(),
        )
        await self._publish_harness_record(record)

    async def run_task(self, task: str) -> list[ToolResponse]:
        with start_run_span(self.run_id, agent_id=self.agent_id):
            await self._publish_harness_record(self.harness.run_started(run_id=self.run_id, agent_id=self.agent_id))
            await self._publish_harness_record(
                self.harness.agent_session_started(
                    run_id=self.run_id,
                    agent_id=self.agent_id,
                    agent_instance_id=self.agent_instance_id,
                )
            )
            results: list[ToolResponse] = []
            messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
            active_step_id: str | None = None
            active_step_closed = False
            try:
                for turn in range(1, self.max_model_turns + 1):
                    step_id = self._step_id(turn)
                    active_step_id = step_id
                    active_step_closed = False
                    with start_model_step_span(self.run_id, step_id, agent_id=self.agent_id):
                        await self._publish_harness_record(
                            self.harness.model_step_started(
                                run_id=self.run_id,
                                agent_id=self.agent_id,
                                agent_instance_id=self.agent_instance_id,
                                step_id=step_id,
                            )
                        )
                        response = await self.client.messages.create(
                            model=os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5"),
                            max_tokens=int(os.getenv("ANTHROPIC_MAX_TOKENS", "16000")),
                            system=self.registration.role_prompt,
                            tools=self.tools,
                            messages=messages,
                        )
                        if getattr(response, "stop_reason", None) in {"refusal", "max_tokens"}:
                            raise RuntimeError(f"model stopped with stop_reason={response.stop_reason}")
                        content = list(response.content)
                        tool_uses = [block for block in content if getattr(block, "type", None) == "tool_use"]
                        if not tool_uses:
                            await self._record_model_step_end(step_id, "completed")
                            active_step_closed = True
                            await self._record_session_end("completed")
                            return results

                        messages.append({"role": "assistant", "content": [_anthropic_content_block(block) for block in content]})
                        tool_result_blocks: list[dict[str, Any]] = []
                        traceparent = self._traceparent_for_step(step_id)
                        for block in tool_uses:
                            tool_call_id = str(block.id)
                            arguments = dict(block.input)
                            with start_tool_call_span(self.run_id, tool_call_id, tool_name=str(block.name), traceparent=traceparent):
                                self.harness.tool_intent(
                                    run_id=self.run_id,
                                    agent_id=self.agent_id,
                                    agent_instance_id=self.agent_instance_id,
                                    step_id=step_id,
                                    tool_call_id=tool_call_id,
                                    tool_name=str(block.name),
                                    arguments=arguments,
                                    traceparent=traceparent,
                                )
                                tool_response = await self.ask_tool(
                                    str(block.name),
                                    arguments,
                                    step_id=step_id,
                                    tool_call_id=tool_call_id,
                                    traceparent=traceparent,
                                )
                            results.append(tool_response)
                            tool_result_blocks.append(_tool_result_block(tool_call_id, tool_response))

                        messages.append({"role": "user", "content": tool_result_blocks})
                        # One decoy hit earns a corrected attempt; a second one fails the session.
                        if sum(result.reason_code == DECOY_REASON_CODE for result in results) > 1:
                            raise RepeatedDecoyInvocation("repeated decoy tool invocations")
                        if turn == self.max_model_turns:
                            raise MaxModelTurnsExceeded(f"reached max model turns: {self.max_model_turns}")
                        await self._record_model_step_end(step_id, "completed")
                        active_step_closed = True
            except MaxModelTurnsExceeded as exc:
                if active_step_id is not None and not active_step_closed:
                    await self._record_model_step_end(active_step_id, "exhausted", str(exc))
                await self._record_session_end("exhausted", str(exc))
                raise
            except asyncio.CancelledError:
                if active_step_id is not None and not active_step_closed:
                    await self._record_model_step_end(active_step_id, "cancelled")
                await self._record_session_end("cancelled")
                raise
            except Exception as exc:
                if active_step_id is not None and not active_step_closed:
                    await self._record_model_step_end(active_step_id, "failed", str(exc))
                await self._record_session_end("failed", str(exc))
                raise

            return results

    async def _record_model_step_end(self, step_id: str, status: LifecycleStatus, error: str | None = None) -> None:
        await self._publish_harness_record(
            self.harness.model_step_ended(
                run_id=self.run_id,
                agent_id=self.agent_id,
                agent_instance_id=self.agent_instance_id,
                step_id=step_id,
                status=status,
                error=error,
            )
        )

    async def _record_session_end(self, status: LifecycleStatus, error: str | None = None) -> None:
        await self._publish_harness_record(
            self.harness.agent_session_ended(
                run_id=self.run_id,
                agent_id=self.agent_id,
                agent_instance_id=self.agent_instance_id,
                status=status,
                error=error,
            )
        )
        await self._publish_harness_record(
            self.harness.run_ended(
                run_id=self.run_id,
                agent_id=self.agent_id,
                status=status,
                error=error,
            )
        )

    async def _publish_harness_record(self, record: HarnessRecord) -> None:
        if self.nc is None or not hasattr(self.nc, "publish"):
            return
        await self.nc.publish(
            agent_lifecycle_subject(record.run_id, record.agent_id or self.agent_id),
            encode_signed_harness_record(record),
        )

    def _step_id(self, turn: int) -> str:
        return f"{self.agent_instance_id}-turn-{turn}"

    def _traceparent_for_step(self, step_id: str) -> str:
        parent_id = hashlib.sha256(step_id.encode("utf-8")).hexdigest()[:16]
        return f"00-{self.trace_id}-{parent_id}-01"


class Agent(AnthropicHarnessAdapter):
    pass


def _anthropic_content_block(block: Any) -> dict[str, Any]:
    block_type = getattr(block, "type", None)
    if block_type == "text":
        return {"type": "text", "text": str(block.text)}
    if block_type == "tool_use":
        return {
            "type": "tool_use",
            "id": str(block.id),
            "name": str(block.name),
            "input": dict(block.input),
        }
    if isinstance(block, dict):
        return block
    if block_type in {"thinking", "redacted_thinking"}:
        # Thinking blocks must be echoed back unchanged alongside their tool_use blocks.
        return block.model_dump(exclude_none=True)
    raise TypeError(f"unsupported Anthropic content block: {block_type}")


def _tool_result_block(tool_call_id: str, response: ToolResponse) -> dict[str, Any]:
    if response.error:
        content = {"error": response.error, "reason": response.reason, "allowed": response.allowed}
        return {
            "type": "tool_result",
            "tool_use_id": tool_call_id,
            "content": json.dumps(content, sort_keys=True),
            "is_error": True,
        }
    return {
        "type": "tool_result",
        "tool_use_id": tool_call_id,
        "content": json.dumps(response.result, sort_keys=True),
    }


async def run(agent_id: str, run_id: str, task: str) -> None:
    agent = Agent(agent_id, run_id)
    await agent.connect()
    try:
        results = await agent.run_task(task)
        print(json.dumps([result.model_dump() for result in results], indent=2, default=str))
    finally:
        assert agent.nc is not None
        await agent.nc.drain()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("agent_id", choices=load_agent_registry().enabled_agent_ids())
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task", required=True)
    args = parser.parse_args()
    asyncio.run(run(args.agent_id, args.run_id, args.task))


if __name__ == "__main__":
    main()
