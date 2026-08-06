"""Reasoner seam: the event vocabulary is stable and strategies are swappable.

Verifies that ``ReActReasoner`` produces the exact event stream the runners
consume, and that ``QueryEngine.ask`` can swap in a custom reasoning strategy
without touching the transport, budget, or tool layers.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from modus.agent.query_engine import QueryEngine
from modus.agent.strategies.react import ReActReasoner
from modus.config import ModusConfig
from modus.tools.base import Tool, ToolContext, ToolResult, object_schema
from modus.tools.registry import ToolRegistry
from modus.types import Message


class FakeClient:
    def __init__(self, *, tool_turn: bool = False) -> None:
        self.model_name = "fake"
        self.provider_name = "test"
        self.max_context_window = 128_000
        self.tool_turn = tool_turn

    async def chat(self, messages, tools, *, system_prompt):
        if self.tool_turn and not any(m.role == "tool" for m in messages):
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0, "id": "t1",
                    "function": {"name": "echo", "arguments": '{"text":"hi"}'},
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        yield {"type": "text_delta", "text": "answer"}
        yield {"type": "usage", "usage": {"input_tokens": 3, "output_tokens": 2}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


async def _echo(payload, _ctx):
    return ToolResult(str(payload.get("text") or ""))


def _registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(Tool(
        name="echo", description="echo",
        parameters=object_schema({"text": {"type": "string"}}, ["text"]),
        handler=_echo,
    ))
    return r


def _reasoner(**kwargs) -> ReActReasoner:
    cfg = ModusConfig()
    return ReActReasoner(
        llm_client=FakeClient(tool_turn=kwargs.pop("tool_turn", False)),
        tool_registry=_registry(),
        system_prompt="sys",
        cwd="/tmp",
        config=cfg,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_react_reasoner_emits_canonical_event_vocabulary():
    reasoner = _reasoner(tool_turn=True)
    messages = [Message(role="user", content="hi")]
    events = [ev async for ev in reasoner.run(messages)]
    types = [ev["type"] for ev in events]
    # Tool-use round trip then final answer.
    assert "tool_call" in types
    assert "tool_result" in types
    assert "text_delta" in types
    assert types[-1] == "done"
    done = events[-1]
    assert done["stop_reason"] == "completed"
    assert any(m.role == "tool" for m in done["messages"])  # tool result appended


@pytest.mark.asyncio
async def test_react_reasoner_plain_text_run():
    reasoner = _reasoner()
    events = [ev async for ev in reasoner.run([Message(role="user", content="hi")])]
    assert events[-1]["type"] == "done"
    assert events[-1]["stop_reason"] == "completed"
    text = "".join(ev["text"] for ev in events if ev["type"] == "text_delta")
    assert text == "answer"


@pytest.mark.asyncio
async def test_query_engine_accepts_custom_reasoner_factory():
    captured: dict[str, Any] = {}

    class CustomReasoner:
        """A stand-in for a future AGI reasoning strategy."""

        def __init__(self, **kwargs):
            captured["factory_kwargs"] = kwargs

        async def run(self, messages, *, approval_callback=None, cancel_event=None):
            captured["messages"] = messages
            yield {"type": "text_delta", "text": "custom-agi"}
            yield {"type": "done", "total_turns": 1, "total_tokens": 2,
                   "messages": messages, "stop_reason": "completed",
                   "budget": {}, "verification": {}}

    engine = QueryEngine(
        llm_client=FakeClient(),
        tool_registry=_registry(),
        config=ModusConfig(),
        cwd="/tmp",
    )
    events = [ev async for ev in engine.ask(
        "hello", reasoner_factory=CustomReasoner,
    )]
    assert events[-1]["type"] == "done"
    assert events[-1]["stop_reason"] == "completed"
    text = "".join(ev["text"] for ev in events if ev["type"] == "text_delta")
    assert text == "custom-agi"
    assert captured["messages"][0].content == "hello"


@pytest.mark.asyncio
async def test_default_reasoner_is_react():
    engine = QueryEngine(
        llm_client=FakeClient(),
        tool_registry=_registry(),
        config=ModusConfig(),
        cwd="/tmp",
    )
    events = [ev async for ev in engine.ask("hi")]
    assert events[-1]["type"] == "done"
    # Default path still produces the classic ReAct answer.
    assert "".join(ev["text"] for ev in events if ev["type"] == "text_delta") == "answer"


# ─── Context assembly seam ────────────────────────────────────────────────

class _StubSession:
    def __init__(self, *, db_id=None, main_history=None):
        self.db_id = db_id
        self.main_history = main_history or []


def test_context_provider_assembles_memory_history_and_skill():
    from modus.agent.context import SessionContextProvider
    from modus.desktop import db, memory
    from modus.types import Message as M

    sid = db.create_session("ctx-seam")["id"]
    memory.add_memory(sid, "项目使用 pytest 与分层记忆", category="fact")
    provider = SessionContextProvider()
    session = _StubSession(
        db_id=sid,
        main_history=[M(role="assistant", content="prior")],
    )
    history = provider.effective_history(
        session,
        transient=[M(role="tool", content="t", tool_call_id="c1")],
        skill_message=type("Skill", (), {"content": "使用 pytest"}),  # noqa: B950
    )
    contents = [m.content for m in history]
    # Session history first, then transient, then skill, then memory as system.
    assert "prior" in contents
    assert "t" in contents
    assert any("pytest" in c for c in contents)
    assert any(m.role == "system" for m in history)


def test_context_provider_does_not_require_message_param():
    """The protocol and impl agree: the user's new message is added by the runner."""
    from modus.agent.context import ContextProvider, SessionContextProvider

    # Protocol must not force a ``message`` keyword the impl does not accept.
    sig = ContextProvider.effective_history.__annotations__
    assert "message" not in sig
    # And the impl rejects the protocol's old signature, catching drift.
    with pytest.raises(TypeError):
        SessionContextProvider().effective_history(_StubSession(), message="hi")

