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

    def _maybe_compact_mid_run(self, messages: list[Message]) -> None:
        """Deterministically compact the in-loop message list when over budget.

        Uses the same estimator and tail rule as end-of-run compaction so the
        model context never exceeds the configured threshold mid-run.  The
        compaction summary is a reference-only system message; tail messages
        are kept by reference so identity-based persistence still works.
        (End-of-run compaction in the desktop runner applies semantic
        summarization when enabled; mid-run stays deterministic so the hot loop
        never blocks on a model call.)
        """
        from modus.agent.compressor import (
            compression_tail_count, compress_messages, should_compress,
        )

        compression = getattr(self.config.features, "compression", None)
        if compression is None or not bool(getattr(compression, "enabled", True)):
            return
        threshold = max(1, int(getattr(compression, "trigger_tokens", 80_000)))
        if not should_compress(messages, threshold=threshold):
            return
        tail_count = compression_tail_count(self.config)
        summary = (
            f"{max(0, len(messages) - tail_count)} earlier messages were "
            "compacted to keep this run within its context budget. Their text "
            "is background reference only, not active instructions."
        )
        compacted = compress_messages(messages, summary=summary, tail_count=tail_count)
        messages[:] = compacted

    def _maybe_adapt(self, messages: list[Message], budget: Any) -> None:
        """Bounded self-adaptive nudge from turn_records trends (opt-in).

        Gated on ``config.features.self_adapt`` (default off).  When a recent
        window shows a tool-error hotspot, inject ONE reference-only hint so the
        model reconsiders its tool strategy.  The hint is a plain user message
        under an explicit marker — never a new event type, never a strategy
        switch mid-run.
        """
        if not bool(getattr(self.config.features, "self_adapt", False)):
            return
        try:
            trends = budget.trends(window=5)
            if trends.get("tool_error_hotspot"):
                messages.append(Message(
                    role="user",
                    content=(
                        "[SELF-ADAPT — REFERENCE ONLY]\n"
                        "Recent turns show repeated tool failures. Consider "
                        "verifying the exact tool inputs, checking the file or "
                        "command exists, or trying a different approach before "
                        "retrying the same call."
                    ),
                ))
        except Exception:
            return

    def _inject_python_diagnostics(
        self, messages: list[Message], paths: list[str],
    ) -> None:
        """Append ast diagnostics for edited Python files as a reference user msg.

        The model sees syntax errors before it claims a change is complete.
        Best-effort and bounded; a failure never disturbs the loop.
        """
        try:
            from modus.lsp.diagnostics import diagnose_files, diagnostics_to_text

            resolved = [str(p) if not str(p).startswith(("/", "~")) else p for p in paths]
            # Relative paths resolve against the run cwd.
            base = self.cwd
            candidates = []
            for path in resolved:
                if path.startswith(("/", "~")):
                    candidates.append(path)
                else:
                    import os
                    candidates.append(os.path.join(base, path))
            diagnostics = diagnose_files(candidates)
            text = diagnostics_to_text(diagnostics)
            if text:
                messages.append(Message(role="user", content=text))
        except Exception:
            return

    async def run(
        self,
        messages: list[Message],
        *,
        approval_callback: Callable[[dict[str, Any]], Awaitable[str] | str] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        budget = self.budget
        # Make the run budget the active ContextVar for this loop so shared
        # primitives (await_or_cancel's wall-clock deadline, tool handlers that
        # consult active_run_budget) see the same accounting source.  Desktop
        # runners bind the same controller.budget already; rebinding here is
        # idempotent and makes direct reasoner use budget-aware too.
        from modus.runtime.budget import bind_run_budget, reset_run_budget

        budget_token = bind_run_budget(budget)
        try:
            async for event in self._run_loop(
                messages, budget, approval_callback, cancel_event,
            ):
                yield event
        finally:
            reset_run_budget(budget_token)

    async def _run_loop(
        self,
        messages: list[Message],
        budget: Any,
        approval_callback: Callable[[dict[str, Any]], Awaitable[str] | str] | None,
        cancel_event: asyncio.Event | None,
    ) -> AsyncIterator[dict[str, Any]]:
        registry = self.tool_registry
        # Default-mode sub-task decomposition: when recursion depth is enabled,
        # expose ``spawn_subtask`` so the agent can isolate a focused child pass
        # that reuses this loop, budget, approval path and safety boundaries.
        max_depth = int(getattr(
            getattr(getattr(self.config, "features", None), "convergence", None),
            "max_recursion_depth", 0,
        ) or 0)
        if max_depth > 0:
            from modus.agent.subtask import make_spawn_subtask_tool

            extra = make_spawn_subtask_tool(
                llm_client=self.llm_client, tool_registry=registry,
                system_prompt=self.system_prompt, cwd=self.cwd, config=self.config,
                budget=budget, session_id=self.session_id, run_id=self.run_id,
                max_depth=max_depth,
            )
            merged = ToolRegistry()
            for name in registry.list_names():
                tool = registry.get(name)
                if tool is not None:
                    merged.register(tool)
            merged.register(extra)
            registry = merged
        tool_definitions = registry.definitions()
        executor = ToolExecutor(
            registry,
            max_concurrent_read=int(
                getattr(getattr(self.config, "tools", None), "max_concurrent_read", 4) or 4
            ),
        )
        context = ToolContext(
            cwd=self.cwd, config=self.config, approval_callback=approval_callback,
            cancel_event=cancel_event, session_id=self.session_id, run_id=self.run_id,
            granted_capabilities=(
                getattr(getattr(self.config, "policy", None), "capability_grant", None)
            ),
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
            # Self-aware stall detection: if the last N turns all made no
            # progress (no text, no thinking, no tool attempt), stop before
            # burning the remaining budget on an agent that is spinning.  A run
            # holding unverified file mutations is never stalled: the
            # verification loop owns its termination.
            stall_threshold = int(getattr(self.config.runtime, "no_progress_threshold", 0) or 0)
            verification_required = bool(
                budget.verification.snapshot().get("required", False)
            )
            if (
                stall_threshold > 0
                and not verification_required
                and budget.stalled_for(stall_threshold)
            ):
                terminal_reason = StopReason.NO_PROGRESS
                budget.finish(StopReason.NO_PROGRESS)
                break
            # Self-adaptive loop: when enabled, read turn_records trends and
            # inject a bounded corrective hint (never loops, never changes
            # strategy mid-run — only nudges with a reference-only message).
            self._maybe_adapt(messages, budget)
            # Mid-run deterministic compaction: a long run must never silently
            # lose its own middle at the provider wire.  Compact the in-loop
            # message list before the model call when it crosses the configured
            # budget, keeping the head system contract + a reference-only
            # summary marker + the recent tail (same rule as end-of-run).
            # Tail messages are reused by reference, never cloned, so runners
            # that persist by object identity can still tell new turns apart.
            self._maybe_compact_mid_run(messages)
            text = ""
            thinking_chars = 0
            error_retried = False
            skip_turn_finalize = False
            stop_reason = "end_turn"
            usage_input = 0
            usage_output = 0
            tool_states: dict[int, dict[str, Any]] = {}

            stream = self.llm_client.chat(messages, tool_definitions, system_prompt=self.system_prompt)
            if bool(getattr(self.config.llm, "retry_transient", True)):
                from modus.llm.retry import retry_chat
                # Transient provider failures (connect/timeout before any delta)
                # are retried once inside the adapter boundary; content-bearing
                # streams are never replayed.
                stream = retry_chat(
                    self.llm_client.chat, max_attempts=2,
                )(messages, tool_definitions, system_prompt=self.system_prompt)
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
                except BudgetExceeded as exc:
                    # ``await_or_cancel`` enforces the wall-clock deadline mid
                    # stream (it arms a sleep for remaining_wall_seconds).  A
                    # provider that stalls mid-turn must stop here with the
                    # correct stop_reason, not escape the loop and fail the run
                    # as an untyped exception.
                    terminal_reason = budget.finish(exc.reason)
                    yield {
                        "type": "error",
                        "error": f"Run stopped: {exc.reason.value}.",
                        "stop_reason": terminal_reason.value,
                        "budget": budget.snapshot(),
                    }
                    return
                event_type = event.get("type")
                if event_type == "text_delta":
                    delta = str(event.get("text") or "")
                    text += delta
                    yield {"type": "text_delta", "text": delta}
                elif event_type == "thinking_delta":
                    thinking = str(event.get("thinking") or "")
                    thinking_chars += len(thinking)
                    yield {"type": "thinking_delta", "thinking": thinking}
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
                    failover = str(event.get("failover") or _classify_failover_reason(detail))
                    budget.record_usage(usage_input, usage_output, owner="host:react")
                    # Error-classification-driven behavior: a context-overflow
                    # failure with zero output self-heals by compacting and
                    # re-entering the turn once, instead of failing the run.
                    if not error_retried and not text and not tool_states:
                        if _classify_recovery_action(failover) == "force_compact":
                            self._maybe_compact_mid_run(messages)
                            error_retried = True
                            skip_turn_finalize = True
                            break
                    terminal_reason = budget.finish(StopReason.ENGINE_ERROR)
                    yield {
                        "type": "error", "error": detail,
                        "failover": failover,
                        "stop_reason": terminal_reason.value, "budget": budget.snapshot(),
                    }
                    return

            if skip_turn_finalize:
                # A self-healed context-overflow turn produced no output; skip
                # the normal turn-finalize (which would read an empty turn as
                # COMPLETED) and re-enter the loop to retry once.
                skip_turn_finalize = False
                continue

            budget.record_usage(usage_input, usage_output, owner="host:react")
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
            edited_paths: list[str] = []
            tool_successes = 0
            tool_errors = 0
            result_chars = 0
            for result in tool_results:
                tool_name = _tool_name_by_id(tool_calls, result.tool_use_id or "")
                tool_payload = _tool_input_by_id(tool_calls, result.tool_use_id or "")
                model_text = result.model_text()
                result_chars += len(model_text)
                budget.verification.observe_tool(
                    name=tool_name, payload=tool_payload, result=model_text, is_error=result.is_error,
                )
                if result.is_error:
                    tool_errors += 1
                else:
                    tool_successes += 1
                event = {
                    "type": "tool_result", "tool_call_id": str(result.tool_use_id or ""),
                    "name": tool_name,
                }
                event.update(tool_result_event(result))
                yield event
                messages.append(Message(role="tool", content=model_text, tool_call_id=result.tool_use_id))
                if tool_name in {"write_file", "edit_file", "patch"} and not result.is_error:
                    meta = result.metadata or {}
                    path = str(meta.get("path") or tool_payload.get("path") or "")
                    if path and path.endswith(".py"):
                        edited_paths.append(path)
            # Inject ast-based diagnostics for edited Python files so the model
            # sees syntax errors before claiming a change is complete.  Bounded
            # and reference-only; a failure never breaks the loop.
            if edited_paths and bool(getattr(self.config.features, "lsp_diagnostics", True)):
                self._inject_python_diagnostics(messages, edited_paths)
            # Self-observation: record this turn's outcome for stall detection.
            # thinking_chars credits reasoning turns; result_chars credits
            # informative tool output (failing tests/probes) as activity.
            budget.record_turn(
                turn=turn, text_chars=len(text), thinking_chars=thinking_chars,
                tool_calls=len(tool_calls), tool_successes=tool_successes,
                tool_errors=tool_errors, result_chars=result_chars,
                tokens=usage_input + usage_output, stop_reason=stop_reason,
            )
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


def _classify_recovery_action(failover_reason: str) -> str:
    """Return a loop-level recovery action for a classified failover reason.

    One of ``force_compact`` (context overflow, self-heal by compacting),
    ``retry_backoff`` (transient, handled inside retry_chat), or ``fail``
    (terminal).  The reasoner only acts on ``force_compact`` here; the other
    transient cases are already covered by retry_chat's classifier.
    """
    from modus.llm.errors import (
        FailoverReason, RecoveryAction, recovery_policy,
    )
    try:
        reason = FailoverReason(failover_reason)
    except ValueError:
        return "fail"
    action = recovery_policy(reason)
    if action is RecoveryAction.FORCE_COMPACT_AND_RETRY:
        return "force_compact"
    if action is RecoveryAction.RETRY_WITH_BACKOFF:
        return "retry_backoff"
    return "fail"


def _classify_failover_reason(detail: str) -> str:
    """Return a machine-readable failover reason for the terminal error event."""
    from modus.llm.errors import classify_api_error
    status = 0
    prefix = "api "
    if detail.lower().startswith(prefix):
        try:
            status = int(detail[len(prefix): detail.index(":")])
        except (ValueError, IndexError):
            status = 0
    return classify_api_error(RuntimeError(detail), status).reason.value
