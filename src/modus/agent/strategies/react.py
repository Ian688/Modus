"""ReAct reasoning strategy: tool-call -> observe -> repeat.

This is the classic agent loop, factored out of the old ``query`` function so
it can be swapped for other strategies.  The event vocabulary is unchanged so
existing runners and tests keep passing.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from modus.agent.query import (
    _finalize_tool_calls, _merge_tool_delta, _tool_input, _tool_input_by_id, _tool_name_by_id,
)
from modus.config import ModusConfig
from modus.llm.base import LlmClient
from modus.runtime.cancellation import RunCancelled, await_or_cancel
from modus.runtime.budget import BudgetExceeded, RunBudget, RunLimits, StopReason
from modus.tools.base import ToolContext
from modus.tools.executor import ToolExecutor
from modus.tools.payload import tool_result_event
from modus.tools.registry import ToolRegistry
from modus.types import Message


class ReActReasoner:
    """Tool-use agent loop: stream, execute, observe, repeat until done."""

    def __init__(
        self,
        *,
        llm_client: LlmClient,
        tool_registry: ToolRegistry,
        system_prompt: str,
        cwd: str,
        config: ModusConfig,
        max_turns: int = 20,
        budget: RunBudget | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.system_prompt = system_prompt
        self.cwd = cwd
        self.config = config
        self.max_turns = max_turns
        self.budget = budget or RunBudget(RunLimits(
            max_turns=max_turns if max_turns != 20 else config.runtime.max_turns,
            max_tokens=config.runtime.max_tokens,
            max_wall_seconds=config.runtime.max_wall_seconds,
            max_verification_attempts=config.runtime.max_verification_attempts,
        ))
        self.session_id = session_id
        self.run_id = run_id

    async def run(
        self,
        messages: list[Message],
        *,
        approval_callback: Callable[[dict[str, Any]], Awaitable[str] | str] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        budget = self.budget
        tool_definitions = self.tool_registry.definitions()
        executor = ToolExecutor(self.tool_registry)
        context = ToolContext(
            cwd=self.cwd, config=self.config, approval_callback=approval_callback,
            cancel_event=cancel_event, session_id=self.session_id, run_id=self.run_id,
        )

        terminal_reason = StopReason.MAX_TURNS

        while True:
            if cancel_event is not None and cancel_event.is_set():
                yield {"type": "error", "error": "Run cancelled before the next model turn."}
                terminal_reason = budget.finish(StopReason.CANCELLED)
                break
            try:
                turn = budget.begin_turn()
            except BudgetExceeded as exc:
                terminal_reason = exc.reason
                break
            text = ""
            stop_reason = "end_turn"
            usage_input = 0
            usage_output = 0
            tool_states: dict[int, dict[str, Any]] = {}

            stream = self.llm_client.chat(messages, tool_definitions, system_prompt=self.system_prompt)
            iterator = stream.__aiter__()
            while True:
                try:
                    event = await await_or_cancel(anext(iterator), cancel_event)
                except StopAsyncIteration:
                    break
                except RunCancelled:
                    terminal_reason = budget.finish(StopReason.CANCELLED)
                    yield {
                        "type": "error", "error": "Run cancelled while waiting for the model.",
                        "stop_reason": terminal_reason.value, "budget": budget.snapshot(),
                    }
                    return
                event_type = event.get("type")
                if event_type == "text_delta":
                    delta = str(event.get("text") or "")
                    text += delta
                    yield {"type": "text_delta", "text": delta}
                elif event_type == "thinking_delta":
                    yield {"type": "thinking_delta", "thinking": event.get("thinking")}
                elif event_type == "tool_call_delta":
                    _merge_tool_delta(tool_states, event["tool_call"])
                elif event_type == "message_end":
                    stop_reason = str(event.get("stop_reason") or "end_turn")
                elif event_type == "usage":
                    usage = event.get("usage") or {}
                    usage_input += int(usage.get("input_tokens") or 0)
                    usage_output += int(usage.get("output_tokens") or 0)
                elif event_type == "error":
                    detail = str(event.get("error") or "Unknown model error")
                    budget.record_usage(usage_input, usage_output)
                    terminal_reason = budget.finish(StopReason.ENGINE_ERROR)
                    yield {
                        "type": "error", "error": detail,
                        "stop_reason": terminal_reason.value, "budget": budget.snapshot(),
                    }
                    return

            budget.record_usage(usage_input, usage_output)
            try:
                budget.check_limits()
            except BudgetExceeded as exc:
                terminal_reason = exc.reason
                break
            tool_calls = _finalize_tool_calls(tool_states)

            assistant_message = Message(role="assistant", content=text, tool_calls=tool_calls)
            messages.append(assistant_message)
            yield {"type": "turn_complete", "turn": turn, "stop_reason": stop_reason}

            if stop_reason != "tool_use" and not tool_calls:
                terminal_reason = StopReason.COMPLETED
                break

            for call in tool_calls:
                name = call.get("function", {}).get("name", "unknown")
                yield {"type": "tool_call", "tool_call_id": str(call.get("id") or ""), "name": name, "input": _tool_input(call)}

            tool_results = await executor.execute_all(tool_calls, context)
            for result in tool_results:
                tool_name = _tool_name_by_id(tool_calls, result.tool_use_id or "")
                tool_payload = _tool_input_by_id(tool_calls, result.tool_use_id or "")
                model_text = result.model_text()
                budget.verification.observe_tool(
                    name=tool_name, payload=tool_payload, result=model_text, is_error=result.is_error,
                )
                event = {
                    "type": "tool_result", "tool_call_id": str(result.tool_use_id or ""),
                    "name": tool_name,
                }
                event.update(tool_result_event(result))
                yield event
                messages.append(Message(role="tool", content=model_text, tool_call_id=result.tool_use_id))
            verification_after_tools = budget.verification.snapshot()
            if verification_after_tools["retry_exhausted"]:
                terminal_reason = StopReason.VERIFICATION_RETRY_LIMIT
                break

        verification = budget.verification.snapshot()
        if terminal_reason is StopReason.COMPLETED:
            if verification["status"] == "failed":
                terminal_reason = budget.finish(StopReason.FAILED)
            elif verification["required"]:
                terminal_reason = budget.finish(StopReason.VERIFICATION_REQUIRED)
            else:
                terminal_reason = budget.finish(StopReason.COMPLETED)
        elif terminal_reason is StopReason.VERIFICATION_RETRY_LIMIT:
            terminal_reason = budget.finish(terminal_reason)
        budget_snapshot = budget.snapshot()
        budget_snapshot["verification"] = verification
        yield {
            "type": "done", "total_turns": budget.turns,
            "total_tokens": budget.total_tokens, "messages": messages,
            "stop_reason": terminal_reason.value, "budget": budget_snapshot,
            "verification": verification,
        }
