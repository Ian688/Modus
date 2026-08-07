"""CLI terminal approval (HITL): rich card + y/N, and ask_complete_async forwarding.

Reuses the fake-LLM + fake-tool pattern from ``test_query_engine_approval_callback``
so the whole approval path (QueryEngine -> Agent -> ReActReasoner -> ToolExecutor
->_approval_error -> callback) is exercised without a real model or network.
"""

from __future__ import annotations

from typing import Any

import pytest

from modus.agent.query_engine import QueryEngine
from modus.config import ModusConfig
from modus.entrypoints.cli import _cli_approval_callback, _format_approval_input
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


def _bash_tool_handler():
    async def handler(_payload, _context):
        return ToolResult("ok")

    return handler


def _make_engine(handler) -> QueryEngine:
    registry = ToolRegistry()
    registry.register(Tool(
        name="bash", description="shell", handler=handler,
        parameters=object_schema({"command": {"type": "string"}}, ["command"]),
        required_keys=["command"], is_read_only=False, danger_level="high", requires_approval=True,
    ))
    return QueryEngine(llm_client=OneToolClient(), tool_registry=registry, config=ModusConfig(), cwd=".")


def _approval_request(**overrides: Any) -> dict:
    request = {
        "tool_name": "bash",
        "tool_call_id": "call-1",
        "description": "shell command",
        "input": {"command": "echo ok"},
        "input_hash": "abc123",
        "approval_expires_at": 9999999999,
        "danger_level": "high",
        "data_disclosure": "none",
    }
    request.update(overrides)
    return request


@pytest.mark.asyncio
async def test_cli_approval_callback_returns_approve_on_y(monkeypatch) -> None:
    monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "y")
    result = await _cli_approval_callback(_approval_request())
    assert result == "approve"


@pytest.mark.asyncio
async def test_cli_approval_callback_returns_deny_on_n(monkeypatch) -> None:
    monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "n")
    result = await _cli_approval_callback(_approval_request())
    assert result == "deny"


@pytest.mark.asyncio
async def test_cli_approval_callback_denies_unknown_answer(monkeypatch) -> None:
    # Anything that is not "y" fails closed.
    monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "maybe")
    result = await _cli_approval_callback(_approval_request())
    assert result == "deny"


def test_format_approval_input_renders_command() -> None:
    text = _format_approval_input({"command": "echo hello", "cwd": "/tmp"})
    assert "echo hello" in text
    assert "/tmp" in text


def test_format_approval_input_redacts_secrets() -> None:
    text = _format_approval_input({"command": "echo x", "api_key": "sk-secret-123"})
    assert "sk-secret-123" not in text
    assert "已隐藏" in text


@pytest.mark.asyncio
async def test_ask_complete_forwards_approval_callback() -> None:
    executed = False

    async def handler(_payload, _context):
        nonlocal executed
        executed = True
        return ToolResult("ok")

    engine = _make_engine(handler)
    result = await engine.ask_complete_async("run", approval_callback=lambda _req: "approve")

    assert executed is True
    assert result.text == "done"


@pytest.mark.asyncio
async def test_ask_complete_deny_blocks_tool() -> None:
    executed = False

    async def handler(_payload, _context):
        nonlocal executed
        executed = True
        return ToolResult("ok")

    engine = _make_engine(handler)
    result = await engine.ask_complete_async("run", approval_callback=lambda _req: "deny")

    # The tool must never execute, and the run still terminates normally.
    assert executed is False
    assert result.text == "done"


@pytest.mark.asyncio
async def test_cli_approval_callback_returns_skip_on_s(monkeypatch) -> None:
    monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "s")
    result = await _cli_approval_callback(_approval_request())
    assert result == "skip"


@pytest.mark.asyncio
async def test_cli_approval_callback_returns_deny_for_unknown(monkeypatch) -> None:
    monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *a, **kw: "x")
    result = await _cli_approval_callback(_approval_request())
    assert result == "deny"


@pytest.mark.asyncio
async def test_cli_approval_callback_modify_returns_approval_response(monkeypatch) -> None:
    from modus.tools.base import ApprovalResponse

    calls = {"n": 0}

    def fake_ask(*a, **kw):
        calls["n"] += 1
        return "m" if calls["n"] == 1 else '{"command":"echo replaced"}'

    monkeypatch.setattr("rich.prompt.Prompt.ask", fake_ask)
    result = await _cli_approval_callback(_approval_request())

    assert isinstance(result, ApprovalResponse)
    assert result.decision == "modify"
    assert result.modified_input == {"command": "echo replaced"}


@pytest.mark.asyncio
async def test_cli_approval_callback_modify_invalid_json_denies(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_ask(*a, **kw):
        calls["n"] += 1
        return "m" if calls["n"] == 1 else "not-json{"

    monkeypatch.setattr("rich.prompt.Prompt.ask", fake_ask)
    result = await _cli_approval_callback(_approval_request())

    assert result == "deny"
