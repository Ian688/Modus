import json

import pytest

from modus.agent.query import query
from modus.config import ModusConfig
from modus.runtime.verification import RunVerification
from modus.runtime.budget import RunBudget, RunLimits
from modus.tools.base import Tool, ToolResult, object_schema
from modus.tools.registry import ToolRegistry


def test_run_verification_requires_a_new_pass_after_each_mutation():
    ledger = RunVerification()
    assert ledger.snapshot()["status"] == "not_required"

    ledger.observe_tool(
        name="edit_file", payload={"path": "app.py"},
        result="Edited app.py: replaced 1 exact match.", is_error=False,
    )
    assert ledger.snapshot()["status"] == "missing"

    passing = json.dumps({"schema": "modus.verification.v1", "status": "passed"})
    ledger.observe_tool(name="run_tests", payload={}, result=passing, is_error=False)
    assert ledger.snapshot()["status"] == "passed"

    ledger.observe_tool(
        name="write_file", payload={"path": "new.py"},
        result="Wrote new.py", is_error=False,
    )
    assert ledger.snapshot()["status"] == "missing"
    assert ledger.snapshot()["required"] is True


def test_run_verification_exposes_bounded_retry_state():
    ledger = RunVerification(max_attempts=2)
    ledger.observe_tool(
        name="edit_file", payload={"path": "app.py"},
        result="Edited app.py: replaced 1 exact match.", is_error=False,
    )
    failed = json.dumps({"schema": "modus.verification.v1", "status": "failed"})
    ledger.observe_tool(name="run_tests", payload={}, result=failed, is_error=True)
    assert ledger.snapshot()["retry_exhausted"] is False
    ledger.observe_tool(name="run_tests", payload={}, result=failed, is_error=True)
    snapshot = ledger.snapshot()
    assert snapshot["attempts"] == 2
    assert snapshot["max_attempts"] == 2
    assert snapshot["retry_exhausted"] is True


@pytest.mark.asyncio
async def test_query_does_not_report_completion_after_unverified_edit():
    class EditThenAnswer:
        calls = 0

        async def chat(self, messages, tools, *, system_prompt):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "tool_call_delta", "tool_call": {
                    "index": 0, "id": "edit-1",
                    "function": {"name": "edit_file", "arguments": '{"path":"app.py"}'},
                }}
                yield {"type": "message_end", "stop_reason": "tool_use"}
                return
            yield {"type": "text_delta", "text": "已修改"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    async def edit_handler(_payload, _context):
        return ToolResult("Edited app.py: replaced 1 exact match.")

    async def approve(_request):
        return "approve"

    registry = ToolRegistry()
    registry.register(Tool(
        name="edit_file", description="edit", handler=edit_handler,
        parameters=object_schema({"path": {"type": "string"}}, ["path"]),
        required_keys=["path"], is_read_only=False, danger_level="safe",
    ))
    events = [event async for event in query(
        llm_client=EditThenAnswer(), tool_registry=registry, system_prompt="system",
        user_message="修改", history=None, cwd=".", config=ModusConfig(), approval_callback=approve,
    )]

    assert events[-1]["type"] == "done"
    assert events[-1]["stop_reason"] == "verification_required"
    assert events[-1]["verification"]["status"] == "missing"


@pytest.mark.asyncio
async def test_query_stops_after_verification_retry_limit():
    class EditAndFailingTests:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools, *, system_prompt):
            self.calls += 1
            tool_messages = [item for item in messages if item.role == "tool"]
            last = str(tool_messages[-1].content) if tool_messages else ""
            if not tool_messages or last.startswith("{\"schema\":"):
                name, args = "edit_file", '{"path":"app.py"}'
            else:
                name, args = "run_tests", '{"command":"false"}'
            yield {"type": "tool_call_delta", "tool_call": {
                "index": 0, "id": f"call-{self.calls}",
                "function": {"name": name, "arguments": args},
            }}
            yield {"type": "message_end", "stop_reason": "tool_use"}

    async def edit_handler(_payload, _context):
        return ToolResult("Edited app.py: replaced 1 exact match.")

    async def tests_handler(_payload, _context):
        return ToolResult(
            json.dumps({"schema": "modus.verification.v1", "status": "failed"}),
            is_error=True,
        )

    async def approve(_request):
        return "approve"

    registry = ToolRegistry()
    registry.register(Tool(
        name="edit_file", description="edit", handler=edit_handler,
        parameters=object_schema({"path": {"type": "string"}}, ["path"]),
        required_keys=["path"], is_read_only=False, danger_level="safe",
    ))
    registry.register(Tool(
        name="run_tests", description="test", handler=tests_handler,
        parameters=object_schema({"command": {"type": "string"}}, ["command"]),
        required_keys=["command"], is_read_only=False, danger_level="safe",
    ))
    budget = RunBudget(RunLimits(max_verification_attempts=2))
    events = [event async for event in query(
        llm_client=EditAndFailingTests(), tool_registry=registry, system_prompt="system",
        user_message="修改", history=None, cwd=".", config=ModusConfig(),
        approval_callback=approve, budget=budget,
    )]

    assert events[-1]["type"] == "done"
    assert events[-1]["stop_reason"] == "verification_retry_limit"
    assert events[-1]["verification"]["attempts"] == 2
    assert events[-1]["verification"]["retry_exhausted"] is True
