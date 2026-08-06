from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from modus.config import ModusConfig
from modus.llm.base import LlmClient
from modus.runtime.budget import RunBudget
from modus.tools.executor import _tool_call_arguments
from modus.tools.registry import ToolRegistry
from modus.types import Message

async def query(
    *,
    llm_client: LlmClient,
    tool_registry: ToolRegistry,
    system_prompt: str,
    user_message: str,
    history: list[Message] | None,
    cwd: str,
    config: ModusConfig,
    max_turns: int = 20,
    budget: RunBudget | None = None,
    approval_callback: Callable[[dict[str, Any]], Awaitable[str] | str] | None = None,
    cancel_event: asyncio.Event | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Thin driver over the default ReAct reasoner.

    Kept for backward compatibility; new callers should use a Reasoner directly
    (``modus.agent.reasoner.Reasoner``) so the reasoning strategy is swappable.
    """
    from modus.agent.strategies import ReActReasoner

    reasoner = ReActReasoner(
        llm_client=llm_client, tool_registry=tool_registry, system_prompt=system_prompt,
        cwd=cwd, config=config, max_turns=max_turns, budget=budget,
        session_id=session_id, run_id=run_id,
    )
    messages = [
        *(history or []),
        Message(role="user", content=user_message),
    ]
    async for event in reasoner.run(
        messages, approval_callback=approval_callback, cancel_event=cancel_event,
    ):
        yield event

# 追加 4 个辅助函数
def _merge_tool_delta(tool_states: dict[int, dict[str, Any]], delta: dict[str, Any]) -> None:
    index = int(delta.get("index") or 0)
    state = tool_states.setdefault(
        index,
        {"id": delta.get("id") or f"tool_{index}", "type": "function", "function": {"name": "", "arguments": ""}},
    )
    if delta.get("id"):
        state["id"] = delta["id"]
    function = delta.get("function") or {}
    if function.get("name"):
        state["function"]["name"] = function["name"]
    if function.get("arguments"):
        state["function"]["arguments"] += function["arguments"]

def _finalize_tool_calls(tool_states: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    calls = []
    for index in sorted(tool_states):
        state = tool_states[index]
        if state["function"]["name"]:
            calls.append(state)
    return calls

def _tool_input(call: dict[str, Any]) -> dict[str, Any]:
    """Use the executor's parser so the tool card matches what will execute."""
    return _tool_call_arguments(call)

def _tool_name_by_id(calls: list[dict[str, Any]], tool_call_id: str) -> str:
    for call in calls:
        if call.get("id") == tool_call_id:
            return str(call.get("function", {}).get("name") or "unknown")
    return "unknown"


def _tool_input_by_id(calls: list[dict[str, Any]], tool_call_id: str) -> dict[str, Any]:
    for call in calls:
        if str(call.get("id") or "") == tool_call_id:
            return _tool_input(call)
    return {}
