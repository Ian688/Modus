"""Generic sub-task tool for the default agent.

The default loop is a single-agent ReAct loop; ``spawn_subtask`` lets it
decompose part of its own scope into a focused child pass that runs an inner
ReAct loop with the *same* model, registry, budget and approval path.  This is
the default-mode analogue of Peri's worker recursion: the child shares every
safety boundary and the parent's budget, but runs in its own subdirectory so
evidence gathering is isolated.

Design notes:
- The child runs a nested ``ReActReasoner`` with the parent's llm client,
  registry, system prompt and budget — no separate model config, no new
  approval channel.
- Depth is bounded by ``max_depth`` so a runaway agent cannot spawn unbounded
  recursion.
- The child works under ``_subtasks/<id>/`` relative to the parent cwd; the
  parent's tools remain scoped to the same workspace.
- Emits a plain tool result; the parent loop continues normally.
"""

from __future__ import annotations

import asyncio
import copy
import os
import uuid
from typing import Any

from modus.tools.base import Tool, ToolContext, ToolResult, object_schema


def _decrement_recursion_depth(config: Any) -> Any:
    """Return a copy of ``config`` whose recursion depth is one lower.

    The child ``ReActReasoner`` reads ``features.convergence.max_recursion_depth``
    to decide whether to expose ``spawn_subtask`` again.  Without this decrement,
    ``max_depth`` is a no-op: every child re-reads the parent's full depth and
    recursion is bounded only by the soft turn/token budget.  A shallow copy is
    not enough — ``features`` is a shared nested dataclass — so the copy is deep.
    A missing or zero depth stays zero (recursion off).
    """
    try:
        child = copy.deepcopy(config)
        depth = int(
            getattr(
                getattr(getattr(child, "features", None), "convergence", None),
                "max_recursion_depth",
                0,
            )
            or 0
        )
        child.features.convergence.max_recursion_depth = max(0, depth - 1)
        return child
    except Exception:
        # Never let the recursion guard fail open: if we cannot copy the config,
        # fall back to a shallow copy with recursion explicitly disabled rather
        # than risking an unbounded chain.
        fallback = copy.copy(config)
        try:
            fallback.features.convergence.max_recursion_depth = 0
        except Exception:
            pass
        return fallback


def make_spawn_subtask_tool(
    *,
    llm_client: Any,
    tool_registry: Any,
    system_prompt: str,
    cwd: str,
    config: Any,
    budget: Any,
    session_id: str | None = None,
    run_id: str | None = None,
    max_depth: int = 2,
    max_turns: int = 5,
) -> Tool:
    """Build a ``spawn_subtask`` tool bound to one agent's execution context."""

    async def _spawn_handler(payload: dict[str, Any], context: ToolContext) -> ToolResult:
        if max_depth <= 0:
            return ToolResult(
                "spawn_subtask 已关闭（max_depth<=0），请在当前上下文直接完成任务。",
                is_error=True,
            )
        description = str(payload.get("description") or "").strip()
        if not description:
            return ToolResult("spawn_subtask requires a 'description'", is_error=True)
        name = str(payload.get("name") or "子任务").strip()
        child_dir = os.path.join(cwd, "_subtasks", uuid.uuid4().hex[:8])
        os.makedirs(child_dir, exist_ok=True)

        from modus.agent.strategies import ReActReasoner
        from modus.types import Message

        child_prompt = (
            f"你正在完成父任务拆分出的子任务。\n"
            f"子任务名称：{name}\n"
            f"子任务描述：{description}\n"
            f"子任务上下文：{str(payload.get('context') or '')[:800]}\n"
            f"成功标准：{str(payload.get('success_criteria') or '')[:400]}\n\n"
            f"在 {child_dir} 下工作，完成任务后给出简洁结果。"
        )
        from modus.runtime.budget import RunBudget, RunLimits

        # The child gets its own stall window so its no-progress turns cannot
        # poison the parent's ledger.  It shares the run's limits, scaled to the
        # child's turn allowance, and merges usage back into the parent budget.
        parent_limits = budget.limits if budget is not None else RunLimits()
        child_budget = RunBudget(RunLimits(
            max_turns=max_turns,
            max_tokens=max(1_000, parent_limits.max_tokens // 2),
            max_wall_seconds=parent_limits.max_wall_seconds,
            max_verification_attempts=parent_limits.max_verification_attempts,
        ))
        reasoner = ReActReasoner(
            llm_client=llm_client,
            tool_registry=tool_registry,
            system_prompt=system_prompt,
            cwd=child_dir,
            config=_decrement_recursion_depth(config),
            max_turns=max_turns,
            budget=child_budget,
            session_id=session_id,
            run_id=run_id,
        )
        text = ""
        async for event in reasoner.run(
            [Message(role="user", content=child_prompt)],
            approval_callback=context.approval_callback,
            cancel_event=context.cancel_event,
        ):
            if event.get("type") == "text_delta":
                text += str(event.get("text") or "")
        if budget is not None:
            budget.record_usage(
                child_budget.input_tokens, child_budget.output_tokens,
            )
            budget.turns = min(
                budget.limits.max_turns, budget.turns + child_budget.turns,
            )
        if not text:
            text = "（子任务无文本输出）"
        return ToolResult(
            text,
            display_summary=f"子任务「{name}」完成",
            metadata={"operation": "spawn_subtask", "path": f"_subtasks/{os.path.basename(child_dir)}"},
        )

    return Tool(
        name="spawn_subtask",
        description=(
            "Decompose part of your scope into a focused child sub-task, run it "
            "in isolation, and return its output. Use when a sub-part of your "
            "task is large enough to warrant its own evidence-gathering pass."
        ),
        parameters=object_schema({
            "name": {"type": "string", "description": "Short name for the child"},
            "description": {"type": "string", "description": "What the child must do"},
            "context": {"type": "string", "description": "Relevant context for the child"},
            "success_criteria": {"type": "string", "description": "How to judge the child's output"},
        }, ["description"]),
        handler=_spawn_handler,
        is_read_only=False,
        is_concurrency_safe=False,
        danger_level="medium",
        requires_approval=False,
        capabilities=("agent",),
    )
