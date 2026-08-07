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



@pytest.mark.asyncio
async def test_react_reasoner_attributes_usage_to_host_ledger():
    """Default loop usage lands in usage_ledger under host:react (MOA/Peri parity)."""
    from modus.agent.strategies import ReActReasoner

    reasoner = ReActReasoner(
        llm_client=FakeClient(),
        tool_registry=_registry(),
        system_prompt="sys",
        cwd="/tmp",
        config=ModusConfig(),
    )
    events = [ev async for ev in reasoner.run([Message(role="user", content="hi")])]
    done = events[-1]

    ledger = done["budget"]["usage_ledger"]
    assert "host:react" in ledger
    assert ledger["host:react"]["input_tokens"] > 0
    # Totals stay authoritative.
    assert done["budget"]["total_tokens"] > 0


@pytest.mark.asyncio
async def test_react_reasoner_enforces_wall_time_mid_stream():
    """A provider that stalls mid-stream stops with stop_reason=wall_time."""
    import asyncio
    from modus.agent.strategies import ReActReasoner
    from modus.runtime.budget import RunBudget, RunLimits

    provider_reaped = asyncio.Event()

    class StallingClient:
        model_name = "test"
        provider_name = "test"
        max_context_window = 8_192

        async def chat(self, messages, tools, *, system_prompt):
            try:
                await asyncio.Event().wait()
                yield {"type": "text_delta", "text": "too late"}
            finally:
                provider_reaped.set()

    budget = RunBudget(RunLimits(max_wall_seconds=0.1))
    reasoner = ReActReasoner(
        llm_client=StallingClient(),
        tool_registry=ToolRegistry(),
        system_prompt="sys",
        cwd="/tmp",
        config=ModusConfig(),
        budget=budget,
    )
    events = [ev async for ev in reasoner.run([Message(role="user", content="wait")])]

    assert provider_reaped.is_set()
    error = next(ev for ev in events if ev["type"] == "error")
    assert error["stop_reason"] == "wall_time"
    assert error["budget"]["stop_reason"] == "wall_time"


@pytest.mark.asyncio
async def test_react_reasoner_compacts_mid_run_over_budget():
    """A long run compacts its in-loop messages before the next model call."""
    from modus.config import CompressionConfig, FeatureConfig
    from modus.agent.strategies import ReActReasoner

    cfg = ModusConfig()
    cfg.features.compression = CompressionConfig(enabled=True, trigger_tokens=100, tail_messages=4)
    cfg.features = FeatureConfig(compression=cfg.features.compression)

    # A client that first emits a tool call, then answers after the tool result,
    # so the loop runs more than one turn and the message list can grow.
    class ToolClient:
        model_name = "fake"
        provider_name = "test"
        max_context_window = 128_000

        async def chat(self, messages, tools, *, system_prompt):
            if not any(m.role == "tool" for m in messages):
                yield {
                    "type": "tool_call_delta", "tool_call": {
                        "index": 0, "id": "t1",
                        "function": {"name": "echo", "arguments": '{"text":"hi"}'},
                    },
                }
                yield {"type": "message_end", "stop_reason": "tool_use"}
                return
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    reasoner = ReActReasoner(
        llm_client=ToolClient(), tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=cfg,
    )
    # Pad the context so the first turn is already over budget.
    padded = [Message(role="user", content="x" * 500) for _ in range(6)]
    events = [ev async for ev in reasoner.run([*padded, Message(role="user", content="go")])]

    done = events[-1]
    assert done["type"] == "done"
    final_messages = done["messages"]
    # Compaction kept the run going and produced a summary marker.
    assert any(
        "[CONTEXT COMPACTION" in str(m.content) for m in final_messages if m.role == "system"
    )
    assert done["stop_reason"] == "completed"


@pytest.mark.asyncio
async def test_identity_persistence_survives_mid_run_compaction():
    """New-turn messages survive mid-run compaction when persisted by identity.

    Mirrors default_runner's pre_run_ids filter: compaction reuses tail messages
    by reference and creates fresh objects for new turns, so identity filtering
    separates what belongs to this run from what was already persisted.
    """
    from modus.config import CompressionConfig, FeatureConfig
    from modus.agent.strategies import ReActReasoner

    cfg = ModusConfig()
    cfg.features = FeatureConfig(compression=CompressionConfig(
        enabled=True, trigger_tokens=100, tail_messages=4,
    ))

    class ToolClient:
        model_name = "fake"
        provider_name = "test"
        max_context_window = 128_000

        async def chat(self, messages, tools, *, system_prompt):
            if not any(m.role == "tool" for m in messages):
                yield {
                    "type": "tool_call_delta", "tool_call": {
                        "index": 0, "id": "t1",
                        "function": {"name": "echo", "arguments": '{"text":"hi"}'},
                    },
                }
                yield {"type": "message_end", "stop_reason": "tool_use"}
                return
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    reasoner = ReActReasoner(
        llm_client=ToolClient(), tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=cfg,
    )
    # Pre-run context is padded enough that the first model call compacts it.
    pre_run = [Message(role="user", content="x" * 400) for _ in range(8)]
    pre_run_ids = {id(m) for m in pre_run}
    events = [ev async for ev in reasoner.run([*pre_run, Message(role="user", content="go")])]

    done = events[-1]
    new_history = [m for m in done["messages"] if id(m) not in pre_run_ids]

    # Compaction happened (summary marker present) and the run's own turns are
    # still identifiable by identity.
    assert any(
        "[CONTEXT COMPACTION" in str(m.content) for m in done["messages"] if m.role == "system"
    )
    assert any(m.role == "tool" for m in new_history)
    assert any(m.role == "assistant" for m in new_history)
    assert any(m.role == "user" and "go" in str(m.content) for m in new_history)


@pytest.mark.asyncio
async def test_react_injects_python_diagnostics_after_edit(tmp_path, monkeypatch):
    """Editing a broken .py file injects ast diagnostics into the next turn."""
    import os
    from modus.agent.strategies import ReActReasoner

    target = tmp_path / "broken.py"
    target.write_text("def ok():\n    return 1\n", encoding="utf-8")

    # A client that first calls write_file with a broken payload, then answers.
    class EditClient:
        model_name = "fake"
        provider_name = "test"
        max_context_window = 128_000

        async def chat(self, messages, tools, *, system_prompt):
            if not any(m.role == "tool" for m in messages):
                yield {
                    "type": "tool_call_delta", "tool_call": {
                        "index": 0, "id": "w1",
                        "function": {"name": "write_file", "arguments": (
                            '{"path":"broken.py","content":"def broken(:\\n"}'
                        )},
                    },
                }
                yield {"type": "message_end", "stop_reason": "tool_use"}
                return
            yield {"type": "text_delta", "text": "fixed now"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    async def write_handler(payload, ctx):
        path = os.path.join(ctx.cwd, payload["path"])
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload["content"])
        from modus.tools.base import ToolResult
        return ToolResult(
            "Wrote file",
            metadata={"operation": "write", "path": "broken.py", "changed": True},
        )

    from modus.tools.base import Tool, ToolResult, object_schema
    registry = ToolRegistry()
    registry.register(Tool(
        name="write_file", description="w", handler=write_handler,
        parameters=object_schema({"path": {}, "content": {}}, ["path", "content"]),
        required_keys=["path", "content"], is_read_only=False, danger_level="medium",
        requires_approval=False,
    ))
    # write_file is medium -> ASK by policy; make it auto-allow.
    from modus.config import PolicyConfig
    cfg = ModusConfig()
    cfg.policy = PolicyConfig(hitl_mode="always" if False else "auto")

    reasoner = ReActReasoner(
        llm_client=EditClient(), tool_registry=registry,
        system_prompt="sys", cwd=str(tmp_path), config=cfg,
    )
    events = [ev async for ev in reasoner.run(
        [Message(role="user", content="edit")],
        approval_callback=lambda _req: "approve",
    )]

    done = events[-1]
    msgs = done["messages"]
    assert any(
        "LSP DIAGNOSTICS" in str(m.content) for m in msgs if m.role == "user"
    )


@pytest.mark.asyncio
async def test_spawn_subtask_available_when_recursion_enabled():
    """max_recursion_depth>0 exposes spawn_subtask in the default loop."""
    from modus.config import ConvergenceConfig, FeatureConfig
    from modus.agent.strategies import ReActReasoner

    cfg = ModusConfig()
    cfg.features = FeatureConfig(convergence=ConvergenceConfig(max_recursion_depth=1))

    class ChildClient:
        model_name = "fake"
        provider_name = "test"
        max_context_window = 128_000

        async def chat(self, messages, tools, *, system_prompt):
            names = [t["function"]["name"] for t in (tools or [])]
            assert "spawn_subtask" in names, "spawn_subtask must be in the tool catalog"
            if not any(m.role == "tool" for m in messages):
                yield {
                    "type": "tool_call_delta", "tool_call": {
                        "index": 0, "id": "sp1",
                        "function": {"name": "spawn_subtask", "arguments": (
                            '{"description":"child work"}'
                        )},
                    },
                }
                yield {"type": "message_end", "stop_reason": "tool_use"}
                return
            yield {"type": "text_delta", "text": "child done"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    reasoner = ReActReasoner(
        llm_client=ChildClient(), tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=cfg,
    )
    events = [ev async for ev in reasoner.run([Message(role="user", content="parent")])]

    types = [ev["type"] for ev in events]
    assert "tool_call" in types
    # The parent then sees the child's text result in a tool_result.
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_failing_tool_probes_do_not_count_as_stall():
    """Repeated failing tool probes are investigative activity, not stall."""
    from modus.config import RuntimeConfig
    from modus.agent.strategies import ReActReasoner

    cfg = ModusConfig()
    cfg.runtime = RuntimeConfig(no_progress_threshold=3)

    class ProbeClient:
        model_name = "fake"
        provider_name = "test"
        max_context_window = 128_000

        async def chat(self, messages, tools, *, system_prompt):
            # Emits a tool call each turn (even though it fails) and no text.
            yield {
                "type": "tool_call_delta", "tool_call": {
                    "index": 0, "id": f"p{len(messages)}",
                    "function": {"name": "echo", "arguments": '{"text":"x"}'},
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}

    async def failing_handler(_payload, _ctx):
        return ToolResult("boom", is_error=True)

    registry = ToolRegistry()
    registry.register(Tool(
        name="echo", description="echo", handler=failing_handler,
        parameters=object_schema({"text": {"type": "string"}}, ["text"]),
        required_keys=["text"],
    ))
    reasoner = ReActReasoner(
        llm_client=ProbeClient(), tool_registry=registry,
        system_prompt="sys", cwd="/tmp", config=cfg, max_turns=6,
    )
    events = [ev async for ev in reasoner.run([Message(role="user", content="go")])]

    done = events[-1]
    # The failing probes are activity: the run burns max_turns, not NO_PROGRESS.
    assert done["stop_reason"] != "no_progress"


@pytest.mark.asyncio
async def test_turn_records_in_budget_snapshot():
    """done carries per-turn self-observation records for debugging."""
    from modus.agent.strategies import ReActReasoner

    reasoner = ReActReasoner(
        llm_client=FakeClient(tool_turn=True), tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
    )
    events = [ev async for ev in reasoner.run([Message(role="user", content="hi")])]

    records = events[-1]["budget"]["turn_records"]
    assert records, "turn_records should be present in the done budget snapshot"
    # The tool round-trip turn attempted a tool call (activity).
    assert any(r["tool_calls"] > 0 for r in records)


@pytest.mark.asyncio
async def test_context_overflow_self_heals_by_compacting():
    """A context-overflow error with zero output compacts and retries once."""
    from modus.agent.strategies import ReActReasoner

    class OverflowThenOkClient:
        model_name = "fake"
        provider_name = "test"
        max_context_window = 128_000
        calls = 0

        async def chat(self, messages, tools, *, system_prompt):
            OverflowThenOkClient.calls += 1
            if OverflowThenOkClient.calls == 1:
                yield {"type": "error", "error": "API 413: context length exceeded"}
                return
            yield {"type": "text_delta", "text": "recovered after compact"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    reasoner = ReActReasoner(
        llm_client=OverflowThenOkClient(), tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
    )
    events = [ev async for ev in reasoner.run([Message(role="user", content="hi")])]

    assert OverflowThenOkClient.calls == 2  # retried once
    assert events[-1]["type"] == "done"
    assert events[-1]["stop_reason"] == "completed"
    texts = "".join(ev.get("text", "") for ev in events if ev["type"] == "text_delta")
    assert "recovered after compact" in texts


@pytest.mark.asyncio
async def test_auth_error_is_terminal_with_failover_reason():
    """An auth error ends the run immediately with a typed failover reason."""
    from modus.agent.strategies import ReActReasoner

    class AuthClient:
        model_name = "fake"
        provider_name = "test"
        max_context_window = 128_000

        async def chat(self, messages, tools, *, system_prompt):
            yield {"type": "error", "error": "API 401: invalid api key"}
            return

    reasoner = ReActReasoner(
        llm_client=AuthClient(), tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
    )
    events = [ev async for ev in reasoner.run([Message(role="user", content="hi")])]

    error = next(ev for ev in events if ev["type"] == "error")
    assert error["failover"] == "auth"
    assert error["stop_reason"] == "engine_error"
