import pytest

from modus.config import ModusConfig
from modus.tools.base import Tool, ToolContext, ToolResult, object_schema
from modus.tools.executor import ToolExecutor
from modus.tools.registry import ToolRegistry


def _context(callback=None) -> ToolContext:
    return ToolContext(cwd=".", config=ModusConfig(), approval_callback=callback)


def _call(name: str = "tool") -> dict:
    return {"id": "call-1", "function": {"name": name, "arguments": "{}"}}


def _executor(tool: Tool) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    return ToolExecutor(registry)


@pytest.mark.asyncio
async def test_safe_read_only_tool_executes_without_an_approval_callback():
    executed = False

    async def handler(_payload, _context):
        nonlocal executed
        executed = True
        return ToolResult("read")

    tool = Tool("tool", "read", object_schema({}), handler)
    result = (await _executor(tool).execute_all([_call()], _context()))[0]

    assert executed is True
    assert result.content == "read"
    assert result.is_error is False


@pytest.mark.asyncio
async def test_high_risk_tool_without_callback_fails_closed_and_never_executes():
    executed = False

    async def handler(_payload, _context):
        nonlocal executed
        executed = True
        return ToolResult("should not run")

    tool = Tool("tool", "shell", object_schema({}), handler, is_read_only=False, danger_level="high", requires_approval=True)
    result = (await _executor(tool).execute_all([_call()], _context()))[0]

    assert executed is False
    assert result.is_error is True
    assert "approval" in result.content.lower()


@pytest.mark.asyncio
async def test_high_risk_tool_denied_by_callback_never_executes():
    executed = False

    async def handler(_payload, _context):
        nonlocal executed
        executed = True
        return ToolResult("should not run")

    async def deny(request):
        assert request["tool_name"] == "tool"
        assert request["danger_level"] == "high"
        return "deny"

    tool = Tool("tool", "shell", object_schema({}), handler, is_read_only=False, danger_level="high", requires_approval=True)
    result = (await _executor(tool).execute_all([_call()], _context(deny)))[0]

    assert executed is False
    # Wave-3 A2: a human deny is INFORMATION, not an error — the model must see
    # the refusal and redirect, so the tool result carries it as a non-error.
    assert result.is_error is False
    assert "denied" in result.content.lower()


@pytest.mark.asyncio
async def test_high_risk_tool_executes_only_after_callback_approves():
    async def handler(_payload, _context):
        return ToolResult("executed")

    async def approve(_request):
        return "approve"

    tool = Tool("tool", "shell", object_schema({}), handler, is_read_only=False, danger_level="high", requires_approval=True)
    result = (await _executor(tool).execute_all([_call()], _context(approve)))[0]

    assert result == ToolResult(content="executed", tool_use_id="call-1")


@pytest.mark.asyncio
async def test_approval_request_binds_call_id_and_canonical_payload_hash():
    async def handler(_payload, _context):
        return ToolResult("executed")

    requests: list[dict] = []

    async def approve(request):
        requests.append(request)
        return "approve"

    tool = Tool("tool", "shell", object_schema({"a": {}, "b": {}}), handler, is_read_only=False, danger_level="high", requires_approval=True)
    call = {"id": "call-bound", "function": {"name": "tool", "arguments": '{"b":2,"a":1}'}}
    result = (await _executor(tool).execute_all([call], _context(approve)))[0]

    assert result.is_error is False
    assert requests[0]["tool_call_id"] == "call-bound"
    assert requests[0]["input_hash"] == "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
    assert requests[0]["approval_expires_at"] > 0


@pytest.mark.asyncio
async def test_approval_input_mutated_while_waiting_fails_closed():
    executed = False

    async def handler(_payload, _context):
        nonlocal executed
        executed = True
        return ToolResult("should not run")

    async def approve(request):
        request["input"]["command"] = "rm -rf /"
        return "approve"

    tool = Tool("tool", "shell", object_schema({"command": {"type": "string"}}, ["command"]), handler, is_read_only=False, danger_level="high", requires_approval=True)
    call = {"id": "call-tamper", "function": {"name": "tool", "arguments": '{"command":"echo safe"}'}}
    result = (await _executor(tool).execute_all([call], _context(approve)))[0]

    assert executed is False
    assert result.is_error is True
    assert "changed" in result.content.lower()


@pytest.mark.asyncio
async def test_approval_callback_exception_fails_closed():
    async def handler(_payload, _context):
        return ToolResult("should not run")

    async def broken(_request):
        raise RuntimeError("transport disconnected")

    tool = Tool("tool", "shell", object_schema({}), handler, is_read_only=False, danger_level="high", requires_approval=True)
    result = (await _executor(tool).execute_all([_call()], _context(broken)))[0]

    assert result.is_error is True
    assert "approval" in result.content.lower()


@pytest.mark.asyncio
async def test_malformed_tool_arguments_are_rejected_before_approval_callback():
    called = False

    async def handler(_payload, _context):
        return ToolResult("should not run")

    async def callback(_request):
        nonlocal called
        called = True
        return "approve"

    tool = Tool("tool", "read", object_schema({}), handler)
    bad = {"id": "call-1", "function": {"name": "tool", "arguments": "{"}}
    result = (await _executor(tool).execute_all([bad], _context(callback)))[0]

    assert result.is_error is True
    assert called is False
    assert "invalid_json" in result.content


@pytest.mark.asyncio
async def test_approval_modify_executes_with_replacement_payload():
    """A MODIFIED decision re-validates and executes the user-edited payload."""
    from modus.tools.base import ApprovalResponse

    executed_payload = {}

    async def handler(payload, _context):
        nonlocal executed_payload
        executed_payload = dict(payload)
        return ToolResult("modified-ran")

    async def modify(_request):
        return ApprovalResponse.modify({"command": "echo replaced"})

    tool = Tool("tool", "shell", object_schema({"command": {"type": "string"}}, ["command"]),
                handler, is_read_only=False, danger_level="high", requires_approval=True)
    call = {"id": "call-mod", "function": {"name": "tool", "arguments": '{"command":"echo original"}'}}
    result = (await _executor(tool).execute_all([call], _context(modify)))[0]

    assert result.is_error is False
    assert executed_payload == {"command": "echo replaced"}
    assert result.content == "modified-ran"


@pytest.mark.asyncio
async def test_approval_modify_rejects_invalid_replacement():
    """A MODIFIED payload that fails schema re-validation is denied."""
    from modus.tools.base import ApprovalResponse

    executed = False

    async def handler(_payload, _context):
        nonlocal executed
        executed = True
        return ToolResult("should not run")

    async def modify(_request):
        # Missing the required ``command`` key.
        return ApprovalResponse.modify({"other": "x"})

    tool = Tool("tool", "shell", object_schema({"command": {"type": "string"}}, ["command"]),
                handler, is_read_only=False, danger_level="high", requires_approval=True,
                required_keys=["command"])
    call = {"id": "call-mod-bad", "function": {"name": "tool", "arguments": '{"command":"echo ok"}'}}
    result = (await _executor(tool).execute_all([call], _context(modify)))[0]

    assert executed is False
    assert result.is_error is True
    assert "approval-modified" in result.content.lower()


@pytest.mark.asyncio
async def test_approval_skip_is_noop_non_error():
    """A SKIPPED decision does not execute but is not reported as a failure."""
    from modus.tools.base import ApprovalResponse

    executed = False

    async def handler(_payload, _context):
        nonlocal executed
        executed = True
        return ToolResult("should not run")

    async def skip(_request):
        return ApprovalResponse.skip("user chose to skip")

    tool = Tool("tool", "shell", object_schema({}), handler,
                is_read_only=False, danger_level="high", requires_approval=True)
    result = (await _executor(tool).execute_all([_call()], _context(skip)))[0]

    assert executed is False
    assert result.is_error is False
    assert "skipped" in result.content.lower()
