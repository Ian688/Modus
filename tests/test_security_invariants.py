"""Security invariants regression suite.

Locks in the six non-negotiable safety properties of the Modus base so a
future refactor cannot silently weaken them.  Each test maps to one invariant.
"""

from __future__ import annotations

import pytest

from modus.config import ModusConfig
from modus.tools.base import Tool, ToolContext, ToolResult, object_schema
from modus.tools.executor import ToolExecutor, _canonical_hash
from modus.tools.registry import ToolRegistry


def _executor_with(tool: Tool) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return ToolExecutor(registry)


async def _run(executor: ToolExecutor, call: dict, callback=None) -> ToolResult:
    ctx = ToolContext(cwd=".", config=ModusConfig(), approval_callback=callback)
    return (await executor.execute_all([call], ctx))[0]


@pytest.mark.asyncio
async def test_approve_then_execute_ordering():
    """A side-effect tool runs only AFTER an explicit approval, never before."""
    order: list[str] = []

    async def handler(_payload, _ctx):
        order.append("execute")
        return ToolResult("ok")

    async def callback(_request):
        order.append("approve")
        return "approve"

    tool = Tool("tool", "shell", object_schema({}), handler,
                is_read_only=False, danger_level="high", requires_approval=True)
    result = await _run(_executor_with(tool), {"id": "c", "function": {"name": "tool", "arguments": "{}"}}, callback)

    assert order == ["approve", "execute"]
    assert not result.is_error


@pytest.mark.asyncio
async def test_deny_never_executes():
    """A denied tool must never execute its handler."""
    executed = False

    async def handler(_payload, _ctx):
        nonlocal executed
        executed = True
        return ToolResult("bad")

    async def callback(_request):
        return "deny"

    tool = Tool("tool", "shell", object_schema({}), handler,
                is_read_only=False, danger_level="high", requires_approval=True)
    await _run(_executor_with(tool), {"id": "c", "function": {"name": "tool", "arguments": "{}"}}, callback)

    assert executed is False


@pytest.mark.asyncio
async def test_model_input_is_validated_before_execution():
    """Model-provided tool input must pass validation; malformed never runs."""
    called = False

    async def handler(payload, _ctx):
        nonlocal called
        called = True
        return ToolResult("ok")

    async def approve(_req):
        return "approve"

    tool = Tool("tool", "shell", object_schema({"command": {"type": "string"}}, ["command"]),
                handler, is_read_only=False, danger_level="high", requires_approval=True,
                required_keys=["command"])
    # Missing required key -> rejected before approval.
    result = await _run(
        _executor_with(tool),
        {"id": "c", "function": {"name": "tool", "arguments": "{}"}},
        approve,
    )

    assert called is False
    assert result.is_error is True


def test_input_hash_is_stable_and_binding():
    """The approval binds to a canonical hash of the exact payload."""
    assert _canonical_hash({"a": 1, "b": 2}) == _canonical_hash({"b": 2, "a": 1})
    assert _canonical_hash({"a": 1}) != _canonical_hash({"a": 2})


@pytest.mark.asyncio
async def test_snapshot_before_first_mutation():
    """The executor captures a side-git snapshot before the first write tool."""
    import asyncio
    from modus.tools.base import ToolContext

    captured = []

    async def write_handler(payload, ctx):
        return ToolResult("Wrote")

    # A custom executor that records when _capture_pre_turn_snapshot runs.
    executor = _executor_with(Tool(
        "write_file", "w", object_schema({"path": {}, "content": {}}, ["path", "content"]),
        write_handler, is_read_only=False, danger_level="medium", required_keys=["path", "content"],
    ))
    ctx = ToolContext(
        cwd=".", config=ModusConfig(), approval_callback=lambda _r: "approve",
        run_id="run-snapshot-invariant",
    )
    original = executor._capture_pre_turn_snapshot
    executor._capture_pre_turn_snapshot = lambda context: captured.append(True)  # noqa: E731

    try:
        await executor.execute_all(
            [{"id": "w1", "function": {"name": "write_file", "arguments": '{"path":"a.txt","content":"x"}'}}],
            ctx,
        )
    finally:
        executor._capture_pre_turn_snapshot = original

    assert captured == [True]
    assert ctx._snapshot_taken is True
