import asyncio

import pytest

from modus.agent.query import query
from modus.config import ModusConfig
from modus.tools.base import Tool, ToolResult, object_schema
from modus.tools.registry import ToolRegistry


class CancelAfterToolClient:
    model_name = "test"
    provider_name = "test"
    max_context_window = 8_192

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools, *, system_prompt):
        self.calls += 1
        if self.calls == 1:
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "first-call",
                    "function": {"name": "mark_cancelled", "arguments": "{}"},
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        raise AssertionError("cancellation must prevent a second model turn")


@pytest.mark.asyncio
async def test_query_cancellation_after_a_tool_result_prevents_next_model_turn():
    cancelled = asyncio.Event()

    async def mark_cancelled(_payload, _context):
        cancelled.set()
        return ToolResult("cancelled")

    registry = ToolRegistry()
    registry.register(Tool(
        "mark_cancelled", "test tool", object_schema({}), mark_cancelled,
        is_read_only=False, danger_level="safe",
    ))
    client = CancelAfterToolClient()

    async def approve(_request):
        return "approve"

    events = [event async for event in query(
        llm_client=client,
        tool_registry=registry,
        system_prompt="system",
        user_message="run",
        history=None,
        cwd=".",
        config=ModusConfig(),
        approval_callback=approve,
        cancel_event=cancelled,
    )]

    assert client.calls == 1
    assert any(event["type"] == "error" and "cancelled" in event["error"].lower() for event in events)


@pytest.mark.asyncio
async def test_query_cancellation_interrupts_a_stalled_model_stream():
    cancelled = asyncio.Event()
    provider_reaped = asyncio.Event()

    class StalledClient:
        model_name = "test"
        provider_name = "test"
        max_context_window = 8_192

        async def chat(self, messages, tools, *, system_prompt):
            try:
                await asyncio.Event().wait()
                yield {"type": "text_delta", "text": "too late"}
            finally:
                provider_reaped.set()

    async def collect():
        return [event async for event in query(
            llm_client=StalledClient(), tool_registry=ToolRegistry(),
            system_prompt="system", user_message="wait", history=None, cwd=".",
            config=ModusConfig(), cancel_event=cancelled,
        )]

    waiting = asyncio.create_task(collect())
    await asyncio.sleep(0)
    cancelled.set()
    events = await asyncio.wait_for(waiting, timeout=1)

    assert provider_reaped.is_set()
    assert any(event["type"] == "error" and "cancelled" in event["error"].lower() for event in events)
