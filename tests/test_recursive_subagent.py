"""Recursive sub-agents: a worker can spawn its own children via spawn_subtask.

Covers the depth-aware spawn tool, the child-subdirectory isolation, the
depth limit, and the nested task lineage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modus.desktop import peri
from modus.tools.base import ToolContext, ToolResult, object_schema
from modus.tools.registry import ToolRegistry


class RecursiveToolClient:
    """A worker that calls spawn_subtask once, then answers."""

    async def chat(self, messages, tools, *, system_prompt):
        if not any(message.role == "tool" for message in messages):
            assert f"DEPTH: 0" in system_prompt
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0, "id": "spawn-1", "function": {
                        "name": "spawn_subtask",
                        "arguments": '{"name":"child","description":"inspect child","context":"scope","success_criteria":"facts"}',
                    },
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        # After the spawn tool result, produce the final answer.
        yield {"type": "text_delta", "text": "parent result"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class ChildClient:
    async def chat(self, messages, tools, *, system_prompt):
        assert "DEPTH: 1" in system_prompt
        yield {"type": "text_delta", "text": "child result"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def _registry_with_spawn(ctx):
    return peri.build_subagent_tool_registry(
        [], recursive=True, spawn_context=ctx,
    )


class _ConfigStub:
    class _Policy:
        hitl_mode = "auto"
        path_guard_enabled = True
        command_blacklist = []
        audit_log_path = ""

    class _Tools:
        timeout = 60.0
        batch_timeout = 90.0

    policy = _Policy()
    tools = _Tools()


@pytest.mark.asyncio
async def test_execute_subtask_can_spawn_a_child(monkeypatch, tmp_path):
    """A worker with recursion enabled spawns a child and returns its output."""
    captured: dict[str, str] = {}

    # Patch create_llm_client to return the right client per depth: the parent
    # (depth 0) spawns, the child (depth 1) just answers.
    def make_client(_cfg):
        return RecursiveToolClient()

    def make_child_client(_cfg):
        return ChildClient()

    monkeypatch.setattr(peri, "create_llm_client", make_client)
    monkeypatch.setattr(peri, "load_config", lambda: _ConfigStub())

    # Observe the child's cwd/depth by wrapping the real recursion function.
    real_execute = peri.execute_subtask

    async def tracking_execute(subtask, ref_config, user_message, **kwargs):
        if kwargs.get("depth", 0) >= 1:
            captured["child_cwd"] = kwargs.get("cwd", "")
            captured["child_depth"] = kwargs.get("depth")
            # The child answers directly (no spawn), so swap in the child client.
            monkeypatch.setattr(peri, "create_llm_client", make_child_client)
        return await real_execute(subtask, ref_config, user_message, **kwargs)

    # The spawn closure captures the real execute_subtask; route it through
    # our tracker so the child's depth/cwd are observed.
    original_build = peri._make_spawn_subtask_tool

    def patched_build(ctx):
        ctx["execute"] = tracking_execute
        return original_build(ctx)

    monkeypatch.setattr(peri, "_make_spawn_subtask_tool", patched_build)

    output = await real_execute(
        {"description": "parent", "success_criteria": "ok"},
        {"provider": "test", "model": "parent", "api_key": "key"},
        "parent task",
        tool_registry=ToolRegistry(), cwd=str(tmp_path), max_recursion_depth=2,
    )

    assert output == "parent result"
    assert captured["child_depth"] == 1
    child_path = Path(captured["child_cwd"])
    assert child_path.is_relative_to(tmp_path)
    assert child_path.parent.name == "_subtasks"


@pytest.mark.asyncio
async def test_spawn_at_depth_limit_returns_error(monkeypatch, tmp_path):
    """A worker already at max_recursion_depth gets an explicit spawn error."""
    from modus.desktop.peri import _make_spawn_subtask_tool

    ctx = {
        "execute": lambda *a, **k: "child",
        "ref_config": {}, "depth": 2, "max_recursion_depth": 2,
        "cwd": str(tmp_path), "writable": False,
        "cancel_event": None, "approval_callback": None,
        "event_callback": None, "max_turns": 5,
    }
    tool = _make_spawn_subtask_tool(ctx)
    context = ToolContext(cwd=str(tmp_path), config=object())

    result = await tool.execute(
        {"description": "child", "context": "scope", "success_criteria": "facts"},
        context,
    )
    assert result.is_error is True
    assert "递归深度已达上限" in result.content


@pytest.mark.asyncio
async def test_spawn_requires_description(monkeypatch, tmp_path):
    from modus.desktop.peri import _make_spawn_subtask_tool

    ctx = {
        "execute": lambda *a, **k: "child", "ref_config": {},
        "depth": 0, "max_recursion_depth": 2, "cwd": str(tmp_path),
        "writable": False, "cancel_event": None, "approval_callback": None,
        "event_callback": None, "max_turns": 5,
    }
    tool = _make_spawn_subtask_tool(ctx)
    context = ToolContext(cwd=str(tmp_path), config=object())

    result = await tool.execute({"name": "child"}, context)
    assert result.is_error is True
    assert "requires a 'description'" in result.content


def test_persist_task_accepts_depth(monkeypatch, tmp_path):
    """The nested lineage stores depth for recursive children."""
    from modus.desktop import db

    session_id = db.create_session("recursion")["id"]
    run = db.create_run("run-rec", session_id, "peri")
    root = db.create_run_task(
        run_id=run["run_id"], session_id=session_id, ordinal=0, title="root",
        task_kind="root", depth=0,
    )
    child = db.create_run_task(
        run_id=run["run_id"], session_id=session_id, ordinal=0, title="child",
        task_kind="worker", parent_task_id=root["task_id"], depth=1,
    )
    grandchild = db.create_run_task(
        run_id=run["run_id"], session_id=session_id, ordinal=0, title="grandchild",
        task_kind="worker", parent_task_id=child["task_id"], depth=2,
    )
    tasks = db.list_run_tasks(run["run_id"])
    by_title = {t["title"]: t for t in tasks}
    assert by_title["root"]["depth"] == 0
    assert by_title["child"]["depth"] == 1
    assert by_title["child"]["parent_task_id"] == by_title["root"]["task_id"]
    assert by_title["grandchild"]["depth"] == 2
    assert by_title["grandchild"]["parent_task_id"] == by_title["child"]["task_id"]
