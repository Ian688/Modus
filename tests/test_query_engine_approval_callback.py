import pytest

from modus.agent.query_engine import QueryEngine
from modus.config import ModusConfig
from modus.tools.base import Tool, ToolResult, object_schema
from modus.tools.registry import ToolRegistry


class OneToolClient:
    model_name = "test"
    provider_name = "test"

    async def chat(self, messages, tools, *, system_prompt):
        if not any(message.role == "tool" for message in messages):
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0, "id": "danger-1",
                    "function": {"name": "bash", "arguments": '{"command":"echo ok"}'},
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


@pytest.mark.asyncio
async def test_query_engine_forwards_approval_callback_to_agent_run():
    executed = False

    async def handler(_payload, _context):
        nonlocal executed
        executed = True
        return ToolResult("ok")

    async def approve(_request):
        return "approve"

    registry = ToolRegistry()
    registry.register(Tool(
        name="bash", description="shell", handler=handler,
        parameters=object_schema({"command": {"type": "string"}}, ["command"]),
        required_keys=["command"], is_read_only=False, danger_level="high", requires_approval=True,
    ))
    engine = QueryEngine(llm_client=OneToolClient(), tool_registry=registry, config=ModusConfig(), cwd=".")

    events = [event async for event in engine.ask("run", approval_callback=approve)]

    assert executed is True
    assert any(event["type"] == "tool_result" and not event["is_error"] for event in events)
