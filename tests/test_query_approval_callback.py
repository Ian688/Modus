import pytest

from modus.agent.query import query
from modus.config import ModusConfig
from modus.tools.base import Tool, ToolResult, object_schema
from modus.tools.registry import ToolRegistry


class OneToolClient:
    async def chat(self, messages, tools, *, system_prompt):
        if not any(message.role == "tool" for message in messages):
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "danger-1",
                    "function": {"name": "bash", "arguments": '{"command":"echo safe"}'},
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        yield {"type": "text_delta", "text": "finished"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


@pytest.mark.asyncio
async def test_query_forwards_approval_callback_to_the_tool_executor():
    executed = False
    approvals: list[dict] = []

    async def bash(payload, _context):
        nonlocal executed
        executed = True
        assert payload == {"command": "echo safe"}
        return ToolResult("ok")

    async def approve(request):
        approvals.append(request)
        return "approve"

    registry = ToolRegistry()
    registry.register(Tool(
        name="bash", description="shell", handler=bash,
        parameters=object_schema({"command": {"type": "string"}}, ["command"]),
        required_keys=["command"], is_read_only=False, danger_level="high", requires_approval=True,
    ))

    events = [event async for event in query(
        llm_client=OneToolClient(), tool_registry=registry, system_prompt="system",
        user_message="run", history=None, cwd=".", config=ModusConfig(),
        approval_callback=approve,
    )]

    assert executed is True
    assert approvals and approvals[0]["tool_name"] == "bash"
    assert any(event["type"] == "tool_result" and event["is_error"] is False for event in events)
