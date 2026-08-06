import asyncio

import pytest

from modus.config import ModusConfig
from modus.tools.base import Tool, ToolContext, ToolResult, object_schema
from modus.tools.executor import ToolExecutor
from modus.tools.registry import ToolRegistry


def _executor(*tools: Tool) -> ToolExecutor:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return ToolExecutor(registry)


def _call(call_id: str, name: str) -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": "{}"}}


@pytest.mark.asyncio
async def test_cancelled_run_prevents_a_tool_side_effect_from_starting():
    executed = False

    async def handler(_payload, _context):
        nonlocal executed
        executed = True
        return ToolResult("should not run")

    cancelled = asyncio.Event()
    cancelled.set()
    tool = Tool("write", "write", object_schema({}), handler, is_read_only=False, danger_level="medium")
    context = ToolContext(cwd=".", config=ModusConfig(), cancel_event=cancelled)

    result = (await _executor(tool).execute_all([_call("write-1", "write")], context))[0]

    assert executed is False
    assert result.is_error is True
    assert "cancelled" in result.content.lower()


@pytest.mark.asyncio
async def test_cancellation_between_sequential_calls_blocks_the_remaining_side_effect():
    executed: list[str] = []
    cancelled = asyncio.Event()

    async def first(_payload, _context):
        executed.append("first")
        cancelled.set()
        return ToolResult("first")

    async def second(_payload, _context):
        executed.append("second")
        return ToolResult("second")

    async def approve(_request):
        return "approve"

    context = ToolContext(cwd=".", config=ModusConfig(), cancel_event=cancelled, approval_callback=approve)
    results = await _executor(
        Tool("first", "first", object_schema({}), first, is_read_only=False, danger_level="safe"),
        Tool("second", "second", object_schema({}), second, is_read_only=False, danger_level="safe"),
    ).execute_all([_call("first-1", "first"), _call("second-1", "second")], context)

    assert executed == ["first"]
    assert results[0].is_error is False
    assert results[1].is_error is True
    assert "cancelled" in results[1].content.lower()


@pytest.mark.asyncio
async def test_cancellation_after_approval_blocks_the_side_effect():
    """An allowed approval cannot cross a simultaneous cancellation boundary."""
    cancelled = asyncio.Event()
    executed = False

    async def handler(_payload, _context):
        nonlocal executed
        executed = True
        return ToolResult("must not run")

    async def approve(_request):
        cancelled.set()
        return "allow"

    tool = Tool(
        "write", "write", object_schema({}), handler,
        is_read_only=False, danger_level="medium",
    )
    context = ToolContext(
        cwd=".", config=ModusConfig(), cancel_event=cancelled,
        approval_callback=approve,
    )

    result = (await _executor(tool).execute_all([_call("write-1", "write")], context))[0]

    assert executed is False
    assert result.is_error is True
    assert "cancelled" in result.content.lower()
