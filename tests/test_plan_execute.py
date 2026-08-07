"""PlanExecuteReasoner: plan-then-execute emits the canonical event stream."""

from __future__ import annotations

import pytest

from modus.agent.strategies import ReActReasoner
from modus.agent.strategies.plan_execute import PlanExecuteReasoner, _task_messages
from modus.agent.planning import ExecutionPlan, PlanTask
from modus.config import ModusConfig
from modus.types import Message


class PlanClient:
    """Fake LLM: first call returns a plan JSON, later calls return text."""

    model_name = "fake"
    provider_name = "test"
    max_context_window = 128_000
    calls = 0

    async def chat(self, messages, tools, *, system_prompt):
        PlanClient.calls += 1
        if PlanClient.calls == 1:
            # Planning call: no tools, expect plan JSON.
            yield {"type": "text_delta", "text": (
                '{"summary": "重构", "tasks": ['
                '{"id": "task_1", "description": "读缓存代码", "type": "file_read", "dependencies": []},'
                '{"id": "task_2", "description": "改缓存实现", "type": "file_write", "dependencies": ["task_1"]}'
                ']}'
            )}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        yield {"type": "text_delta", "text": f"task output {PlanClient.calls}"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def _registry():
    from modus.tools.base import Tool, object_schema
    from modus.tools.registry import ToolRegistry

    async def echo(payload, _ctx):
        from modus.tools.base import ToolResult
        return ToolResult(str(payload.get("text") or ""))

    registry = ToolRegistry()
    registry.register(Tool(
        name="echo", description="echo",
        parameters=object_schema({"text": {"type": "string"}}, ["text"]),
        handler=echo,
    ))
    return registry


@pytest.mark.asyncio
async def test_plan_execute_emits_plan_then_tasks_then_done():
    PlanClient.calls = 0
    reasoner = PlanExecuteReasoner(
        llm_client=PlanClient(), tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
    )
    events = [ev async for ev in reasoner.run(
        [Message(role="system", content="sys"), Message(role="user", content="重构缓存层")],
    )]

    types = [ev["type"] for ev in events]
    assert "plan" in types
    assert types.count("task_start") == 2
    assert types.count("task_complete") == 2
    done = events[-1]
    assert done["type"] == "done"
    assert done["stop_reason"] == "completed"
    assert done["plan"]["completed"] == ["task_1", "task_2"]
    # Text from both tasks reached the stream.
    texts = "".join(ev.get("text", "") for ev in events if ev["type"] == "text_delta")
    assert "task output 2" in texts
    assert "task output 3" in texts


@pytest.mark.asyncio
async def test_plan_execute_falls_back_to_react_on_bad_plan():
    PlanClient.calls = 0

    class BadPlanClient(PlanClient):
        async def chat(self, messages, tools, *, system_prompt):
            PlanClient.calls += 1
            if PlanClient.calls == 1:
                yield {"type": "text_delta", "text": "not a plan"}
                yield {"type": "message_end", "stop_reason": "end_turn"}
                return
            yield {"type": "text_delta", "text": "react answer"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    reasoner = PlanExecuteReasoner(
        llm_client=BadPlanClient(), tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
    )
    events = [ev async for ev in reasoner.run([Message(role="user", content="hi")])]

    types = [ev["type"] for ev in events]
    assert "plan" not in types
    assert events[-1]["type"] == "done"
    assert events[-1]["stop_reason"] == "completed"
    texts = "".join(ev.get("text", "") for ev in events if ev["type"] == "text_delta")
    assert "react answer" in texts


def test_task_messages_builds_context_with_dependency_results():
    plan = ExecutionPlan(goal="g")
    plan.tasks = [
        PlanTask("t1", "读", "file_read"),
        PlanTask("t2", "写", "file_write", dependencies=["t1"]),
    ]
    outputs = {"t1": "缓存实现在 cache.py"}
    msgs = _task_messages(plan, plan.tasks[1], outputs, [Message(role="system", content="sys")])

    assert msgs[0].role == "system"
    combined = " ".join(str(m.content) for m in msgs)
    assert "cache.py" in combined
    assert "写" in combined


@pytest.mark.asyncio
async def test_agent_mode_plan_selects_plan_execute_reasoner():
    from modus.agent.query_engine import QueryEngine

    PlanClient.calls = 0

    class ModeClient(PlanClient):
        async def chat(self, messages, tools, *, system_prompt):
            PlanClient.calls += 1
            if PlanClient.calls == 1:
                yield {"type": "text_delta", "text": (
                    '{"summary": "s", "tasks": [{"id": "task_1", "description": "one", "type": "analysis"}]}'
                )}
                yield {"type": "message_end", "stop_reason": "end_turn"}
                return
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    cfg = ModusConfig()
    cfg.prompt.agent_mode = "plan"
    engine = QueryEngine(
        llm_client=ModeClient(), tool_registry=_registry(), config=cfg, cwd="/tmp",
    )
    events = [ev async for ev in engine.ask("做一件事")]

    types = [ev["type"] for ev in events]
    assert "plan" in types
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_plan_execute_runs_parallel_tasks_concurrently():
    """Parallelizable tasks run concurrently (wall-clock < serial sum)."""
    import time
    from modus.agent.planning import ExecutionPlan, PlanTask

    PlanClient.calls = 0

    class SlowClient(PlanClient):
        async def chat(self, messages, tools, *, system_prompt):
            PlanClient.calls += 1
            if PlanClient.calls == 1:
                yield {"type": "text_delta", "text": (
                    '{"summary": "p", "tasks": ['
                    '{"id": "a", "description": "read a", "type": "file_read", "dependencies": []},'
                    '{"id": "b", "description": "read b", "type": "file_read", "dependencies": []}'
                    ']}'
                )}
                yield {"type": "message_end", "stop_reason": "end_turn"}
                return
            await asyncio.sleep(0.2)  # each task's inner model call sleeps
            yield {"type": "text_delta", "text": f"done {PlanClient.calls}"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    reasoner = PlanExecuteReasoner(
        llm_client=SlowClient(), tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
    )
    start = time.monotonic()
    events = [ev async for ev in reasoner.run(
        [Message(role="user", content="do both")],
    )]
    elapsed = time.monotonic() - start

    # Two 0.2s tasks running concurrently finish well under 0.4s.
    assert elapsed < 0.38
    assert events[-1]["type"] == "done"
    assert events[-1]["stop_reason"] == "completed"


@pytest.mark.asyncio
async def test_plan_execute_replans_on_failure():
    """A failed task triggers one plan_replan, then the revised plan succeeds."""
    PlanClient.calls = 0

    class ReplanClient(PlanClient):
        async def chat(self, messages, tools, *, system_prompt):
            PlanClient.calls += 1
            if PlanClient.calls == 1:
                # Initial plan: task_a fails, task_b depends on it.
                yield {"type": "text_delta", "text": (
                    '{"summary": "r", "tasks": ['
                    '{"id": "a", "description": "fail this", "type": "file_read", "dependencies": []},'
                    '{"id": "b", "description": "after a", "type": "analysis", "dependencies": ["a"]}'
                    ']}'
                )}
                yield {"type": "message_end", "stop_reason": "end_turn"}
                return
            if PlanClient.calls == 2:
                # Task a fails with a terminal error.
                yield {"type": "error", "error": "API 401: bad key"}
                return
            if PlanClient.calls == 3:
                # Replan call: new single task.
                yield {"type": "text_delta", "text": (
                    '{"summary": "r2", "tasks": ['
                    '{"id": "c", "description": "revised plan", "type": "analysis", "dependencies": []}'
                    ']}'
                )}
                yield {"type": "message_end", "stop_reason": "end_turn"}
                return
            yield {"type": "text_delta", "text": "revised done"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    reasoner = PlanExecuteReasoner(
        llm_client=ReplanClient(), tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
    )
    events = [ev async for ev in reasoner.run([Message(role="user", content="go")])]

    types = [ev["type"] for ev in events]
    assert "plan_replan" in types
    done = events[-1]
    assert done["type"] == "done"
    # The revised plan completed.
    assert done["stop_reason"] == "completed"
    texts = "".join(ev.get("text", "") for ev in events if ev["type"] == "text_delta")
    assert "revised done" in texts


def test_select_reasoner_heuristic():
    from modus.agent.strategies import PlanExecuteReasoner, ReActReasoner
    from modus.agent.strategies.select import select_reasoner
    from modus.config import ModusConfig

    cfg = ModusConfig()  # agent_mode defaults to "react"

    # Conversational stays on ReAct.
    assert select_reasoner("你好", [], cfg) is ReActReasoner
    # Multi-step + file intent -> PlanExecute.
    assert select_reasoner("先读缓存代码，然后重构实现", [], cfg) is PlanExecuteReasoner
    # Explicit factory wins.
    class Custom: pass
    assert select_reasoner("hi", [], cfg, explicit_factory=Custom) is Custom


def test_select_reasoner_agent_mode_plan_pins_plan():
    from modus.agent.strategies import PlanExecuteReasoner
    from modus.agent.strategies.select import select_reasoner
    from modus.config import ModusConfig

    cfg = ModusConfig()
    cfg.prompt.agent_mode = "plan"

    # Even a conversational request goes to PlanExecute when pinned.
    assert select_reasoner("你好", [], cfg) is PlanExecuteReasoner


@pytest.mark.asyncio
async def test_done_messages_include_task_history():
    """done.messages must reflect the accumulated task context, not a pre-run snapshot."""
    PlanClient.calls = 0

    reasoner = PlanExecuteReasoner(
        llm_client=PlanClient(), tool_registry=_registry(),
        system_prompt="sys", cwd="/tmp", config=ModusConfig(),
    )
    events = [ev async for ev in reasoner.run(
        [Message(role="user", content="重构缓存层")],
    )]

    done = events[-1]
    assert done["type"] == "done"
    messages = done["messages"]
    # The task outputs were produced; done.messages must contain more than the
    # pre-run snapshot (which was just the user request).
    assert any("task output" in str(m.content) for m in messages if m.role == "assistant")


@pytest.mark.asyncio
async def test_agent_selects_reasoner_from_current_message():
    """Reasoner selection must use the CURRENT request, not the previous turn."""
    from modus.agent.query_engine import QueryEngine

    PlanClient.calls = 0

    class RecordingClient(PlanClient):
        async def chat(self, messages, tools, *, system_prompt):
            PlanClient.calls += 1
            # Planner calls pass no tools; always return a plan JSON there.
            if not tools:
                yield {"type": "text_delta", "text": (
                    '{"summary": "s", "tasks": [{"id": "t1", "description": "one", "type": "analysis"}]}'
                )}
                yield {"type": "message_end", "stop_reason": "end_turn"}
                return
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    cfg = ModusConfig()
    engine = QueryEngine(
        llm_client=RecordingClient(), tool_registry=_registry(), config=cfg, cwd="/tmp",
    )
    # First turn: conversational "hi" -> ReAct (no plan event).
    events1 = [ev async for ev in engine.ask("hi")]
    assert "plan" not in [ev["type"] for ev in events1]

    # Second turn: multi-step file request -> must select PlanExecute despite
    # the history containing the previous "hi".
    PlanClient.calls = 0
    engine2 = QueryEngine(
        llm_client=RecordingClient(), tool_registry=_registry(), config=cfg, cwd="/tmp",
    )
    async for ev in engine2.ask("hi"):
        pass
    events2 = [ev async for ev in engine2.ask("先重构文件，再创建测试")]

    assert "plan" in [ev["type"] for ev in events2]
