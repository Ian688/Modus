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
