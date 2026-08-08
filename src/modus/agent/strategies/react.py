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
from modus.agent.goal import (
    build_budget_limited_summary_prompt,
    build_goal_steering,
    default_goal_store,
    make_goal_tool,
)
from modus.agent.stall import (
    LEVEL_LOOP, LEVEL_STALL, Ledger, build_stall_context_block,
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
        goal_store: Any = None,
        goal_turns_budget: int | None = None,
        goal_tokens_budget: int | None = None,
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
        # Goal-driven continuation (Wave4 G1).  ``goal_store`` is injected by
        # the caller (defaults to the process-wide store); when a goal is
        # active the loop re-injects steering on idle turns and soft-stops on
        # budget limits instead of hard-stopping.  Explicit per-goal budgets
        # let the caller cap a continuation round independently of the run's
        # own limits; ``None`` means "no per-goal cap" (the run budget still
        # applies).
        self.goal_store = goal_store
        self.goal_turns_budget = goal_turns_budget
        self.goal_tokens_budget = goal_tokens_budget
        self._goal_steered_this_run = False
        # A pending soft-limit summary turn: when the goal crosses its per-goal
        # budget, the loop injects a summarization prompt and lets the model
        # produce a handoff turn before ending with the soft reason.
        self._goal_summary_pending = False
        self._goal_summary_reason: StopReason | None = None
        # Deterministic stall detection (Wave4 G2): a ledger of tool outcomes
        # plus the circuit-breaker state.  Stall detection never hard-breaks —
        # it injects a reference-only ``[STALL DETECTED]`` block and escalates
        # to ``StopReason.STALLED`` only after a sustained ``loop``.  Per-run
        # (fresh per ``run()`` call) so continuation rounds restart clean.
        self._ledger: Ledger | None = None
        self._stall_injected_this_turn = False
        # Wave5 E3 steering (steer vs followUp).  ``steer_queue`` holds user
        # turns that must be injected *during* the run — after the current
        # turn's tool results are replayed back into the message list and
        # before the next LLM call (the same injection window goal-steering and
        # stall context use).  ``followup_queue`` holds turns that wait until
        # the whole run finishes, so the runner can drain them after
        # settlement.  Both queues are session-scoped so a message queued by
        # the WS layer reaches the active (or next) reasoner for this session;
        # a reasoner without a session id uses the process-wide default.
        self.steer_queue = steer_queue_for(self.session_id)
        self.followup_queue = followup_queue_for(self.session_id)
        self._steer_consumed = 0

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

    def _drain_steer_queue(self, messages: list[Message]) -> None:
        """Inject pending steering turns right before the next LLM call.

        Wave5 E3: a steering message must take effect mid-run — after the
        previous turn's tool results are replayed back into the context and
        before the next provider call.  Each queued message is appended as a
        plain user turn (reference/instruction semantics are decided by the
        producer; steering implies "act on this now").  The queue drains FIFO
        so ordering is preserved and no message is re-injected across turns.
        """
        if not self.steer_queue:
            return
        pending = [
            message for message in self.steer_queue
            if id(message) not in {id(existing) for existing in messages}
        ]
        for message in pending:
            messages.append(message)
        self._steer_consumed += len(pending)
        del self.steer_queue[:]

    def drain_followup(self, messages: list[Message]) -> list[Message]:
        """Consume the followup queue after a run has fully finished.

        The runner calls this once per follow-up message so a turn that arrives
        mid-run lands in the NEXT run's initial context instead of being lost.
        Returns the consumed messages.
        """
        if not self.followup_queue:
            return []
        consumed = list(self.followup_queue)
        self.followup_queue.clear()
        messages.extend(consumed)
        return consumed

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

    # ── goal-driven continuation (Wave4 G1) ───────────────────────────────

    def _resolve_goal_store(self) -> Any:
        store = self.goal_store
        if store is None:
            try:
                store = default_goal_store()
            except Exception:
                store = None
        return store

    def _maybe_inject_goal_steering(self, messages: list[Message], cancel_event: asyncio.Event | None) -> None:
        """Inject the ``<goal-steering>`` meta message on an idle turn.

        Runs at the TOP of each loop iteration, before the next LLM call.
        Conditions (CCB's idle hook, ported to a loop-entry check):
        - there is an active (runnable) goal for this session, AND
        - the goal is not already complete/blocked, AND
        - the user is NOT interacting right now (no cancel_event pending, no
          new user turn waiting in the queue).

        The steering message carries objective + token/turn state + Completion
        Audit as a plain reference-only user message — exactly like the
        existing self-adapt / diagnostics hints: it informs the model, it never
        becomes a new instruction channel.
        """
        store = self._resolve_goal_store()
        if store is None:
            return
        # User input priority: once the user cancels or a new message is in
        # flight, auto-continuation yields immediately.
        if cancel_event is not None and cancel_event.is_set():
            return
        # Inject at most once per run: a fresh continuation round re-injects the
        # objective at its start, but re-injecting on every LLM turn would just
        # bloat context with the same block.
        if self._goal_steered_this_run:
            return
        try:
            state = store.active(self.session_id)
        except Exception:
            return
        if state is None:
            return
        # Only steer when the goal is actually continuing; a budget_limited
        # goal gets the summary prompt (handled at the limit), not steering.
        from modus.agent.goal import STATUS_BUDGET_LIMITED, STATUS_MAX_TURNS

        if state.status in (STATUS_BUDGET_LIMITED, STATUS_MAX_TURNS):
            return
        # Avoid piling a second steering message before the model has consumed
        # the one we already injected this iteration.
        if messages and "<goal-steering>" in str(messages[-1].content):
            return
        try:
            steering = build_goal_steering(
                state,
                turns_budget=self.goal_turns_budget,
                tokens_budget=self.goal_tokens_budget,
            )
        except Exception:
            return
        messages.append(Message(role="user", content=steering))
        self._goal_steered_this_run = True

    def _check_goal_limits(self) -> StopReason | None:
        """Return the soft-limit stop reason for this goal, if any.

        ``None`` means the goal is still within its (optional) per-goal budget
        and the loop keeps going.  Crossing a per-goal budget never hard-stops
        a run the way ``BudgetExceeded`` does — the caller records the soft
        state and continues once (the summarization prompt is injected and the
        run ends with the soft reason).
        """
        store = self._resolve_goal_store()
        if store is None:
            return None
        try:
            state = store.get(self.session_id)
        except Exception:
            return None
        if state is None or state.is_terminal():
            return None
        if self.goal_tokens_budget is not None and state.tokens_used >= self.goal_tokens_budget:
            return StopReason.GOAL_BUDGET_LIMITED
        if self.goal_turns_budget is not None and state.turns_executed >= self.goal_turns_budget:
            return StopReason.GOAL_MAX_TURNS
        return None

    def _record_goal_turn(self, tokens: int, turn: int) -> None:
        """Add one executed turn + tokens to the session goal accounting.

        Called once per completed loop turn.  The goal's own per-goal budgets
        are applied here so the soft ``GOAL_*`` states are reached without a
        hard ``BudgetExceeded``.  Token accounting stays single-source: when a
        per-goal token budget is set, ``update_tokens`` owns the counter;
        otherwise ``record_turn`` records it without any transition.
        """
        store = self._resolve_goal_store()
        if store is None:
            return
        try:
            state = store.get(self.session_id)
        except Exception:
            return
        if state is None or state.is_terminal():
            return
        try:
            store.record_turn(
                self.session_id,
                count=1,
                tokens=tokens if self.goal_tokens_budget is None else 0,
                budget_total=self.goal_turns_budget,
            )
        except Exception:
            return
        if self.goal_tokens_budget is not None:
            try:
                store.update_tokens(
                    self.session_id,
                    {"input_tokens": tokens, "output_tokens": 0},
                    total=self.goal_tokens_budget,
                )
            except Exception:
                return

    # ── deterministic stall detection (Wave4 G2) ──────────────────────────

    def _maybe_inject_stall_context(
        self,
        messages: list[Message],
        budget: Any,
    ) -> bool:
        """Check the circuit breaker and inject a ``[STALL DETECTED]`` block.

        Returns True when a block was injected.  The injected message is
        reference-only (exactly like goal-steering / self-adapt hints): it tells
        the model what failing pattern it is repeating and suggests a course
        change — it is NOT a new instruction channel and never hard-breaks the
        loop.  A sustained ``loop`` (or a stall-token cap backstop) escalates
        the run to ``StopReason.STALLED`` at the NEXT loop iteration.
        """
        ledger = self._ledger
        if ledger is None:
            return False
        if self._stall_injected_this_turn:
            return False
        try:
            result = ledger.check_circuit_breaker()
        except Exception:
            return False
        if result.level not in (LEVEL_STALL, LEVEL_LOOP):
            return False
        try:
            block = build_stall_context_block(
                result.level,
                action=result.action,
                count=result.count,
                signature=result.signature,
                pattern=result.pattern or None,
            )
        except Exception:
            return False
        if not block:
            return False
        messages.append(Message(role="user", content=block))
        self._stall_injected_this_turn = True
        return True

    def _stall_escalation_reason(
        self,
        budget: Any,
        *,
        loop_threshold: int,
        no_progress_threshold: int,
        verification_required: bool,
    ) -> StopReason | None:
        """Return ``STALLED`` when the run should hand off to a human, else None.

        Escalation is conservative and deterministic:
        - never while verification is required (the verification loop owns
          recovery), and never when there are too few turns on record;
        - a circuit breaker that stays at ``loop`` for ``loop_threshold``
          consecutive checks;
        - a stalled-pattern token cap backstop (``stall_tokens_exceeded``).
        """
        if verification_required:
            return None
        if not hasattr(budget, "turn_records") or len(budget.turn_records) < 2:
            return None
        ledger = self._ledger
        if ledger is None:
            return None
        try:
            if ledger.consecutive_loops >= loop_threshold:
                return StopReason.STALLED
        except Exception:
            pass
        if no_progress_threshold > 0 and budget.stalled_for(no_progress_threshold):
            return StopReason.STALLED
        try:
            if hasattr(budget, "stall_tokens_exceeded") and budget.stall_tokens_exceeded():
                return StopReason.STALLED
        except Exception:
            pass
        return None

    async def run(
        self,
        messages: list[Message],
        *,
        approval_callback: Callable[[dict[str, Any]], Awaitable[str] | str] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        budget = self.budget
        # Goal steering fires at most once per run (each fresh continuation
        # round re-injects the objective at its start).
        self._goal_steered_this_run = False
        # Deterministic stall detection is per-run: each run() call starts a
        # fresh ledger and a fresh escalation counter.
        self._ledger = Ledger()
        self._stall_injected_this_turn = False
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
        # Goal-driven continuation (Wave4 G1): expose the model-side ``goal``
        # receipt tool so the agent can self-report status.  Declared safe +
        # read-only + memory capability: it never touches files, never needs
        # approval, and only reads/updates the session's own goal state.
        try:
            goal_store = self._resolve_goal_store()
            if goal_store is not None:
                goal_tool = make_goal_tool(store=goal_store)
                merged_registry = ToolRegistry()
                for _name in registry.list_names():
                    _tool = registry.get(_name)
                    if _tool is not None:
                        merged_registry.register(_tool)
                merged_registry.register(goal_tool)
                registry = merged_registry
        except Exception:
            # A goal-tool wiring failure must never break the loop; the goal
            # steering still works without the tool.
            pass
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
            grant_store=_grant_store(self.session_id),
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
            # Goal-driven idle continuation (Wave4 G1): at the TOP of every loop
            # iteration — before the next LLM call — check whether an active
            # goal exists and the user is idle; if so, inject the
            # ``<goal-steering>`` meta message (objective + token state +
            # Completion Audit) so the model keeps attacking instead of treating
            # the budget as a hard stop.  User input priority: a pending cancel
            # or a queued user turn suppresses steering entirely.
            self._maybe_inject_goal_steering(messages, cancel_event)
            # Self-aware stall detection: if the last N turns all made no
            # progress (no text, no thinking, no tool attempt), stop before
            # burning the remaining budget on an agent that is spinning.  A run
            # holding unverified file mutations is never stalled: the
            # verification loop owns its termination.
            stall_threshold = int(getattr(self.config.runtime, "no_progress_threshold", 0) or 0)
            verification_required = bool(
                budget.verification.snapshot().get("required", False)
            )
            # Allow one stall-context injection per loop iteration (a fresh turn
            # may inject again if the pattern persists).
            self._stall_injected_this_turn = False
            if (
                stall_threshold > 0
                and not verification_required
                and budget.stalled_for(stall_threshold)
            ):
                terminal_reason = StopReason.NO_PROGRESS
                budget.finish(StopReason.NO_PROGRESS)
                break
            # Deterministic stall escalation (Wave4 G2): hand the run to a human
            # with a written diagnostic when the circuit breaker has been at
            # ``loop`` across consecutive turns (or the stall-token cap is hit).
            # Conservative: never while verification owns recovery, never before
            # a couple of turns are on record.  ``STALLED`` is an escalation of
            # the soft hints, not a hard loop break.
            loop_escalation_threshold = int(getattr(
                getattr(self.config, "features", None),
                "stall_loop_escalation_threshold", 2,
            ) or 2)
            stall_reason = self._stall_escalation_reason(
                budget,
                loop_threshold=max(1, loop_escalation_threshold),
                no_progress_threshold=stall_threshold,
                verification_required=verification_required,
            )
            if stall_reason is not None:
                terminal_reason = stall_reason
                budget.finish(stall_reason)
                break
            # Deterministic stall context (Wave4 G2): once the circuit breaker
            # reports ``stall``/``loop``, inject a reference-only ``[STALL
            # DETECTED]`` block so the model sees its repeated failing pattern
            # and changes course.  This is an informative nudge — never a hard
            # break and never a new instruction channel.
            self._maybe_inject_stall_context(messages, budget)
            # Wave5 E3 steering: consume pending steer turns right after the
            # previous turn's tool results are back in the context and before
            # the next LLM call — the same injection window goal/stall use.
            self._drain_steer_queue(messages)
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
            # Deterministic stall tokens (Wave4 G2): once the circuit breaker is
            # at ``loop``, this turn's tokens count toward the separate stall
            # counter so a spinning agent is noticed without first burning the
            # whole budget (the cap backstop escalates to ``STALLED``).  The
            # authoritative total is untouched.
            try:
                if (
                    self._ledger is not None
                    and self._ledger.consecutive_loops > 0
                ):
                    budget.record_stall_tokens(
                        usage_input + usage_output, owner="host:react-stall",
                    )
            except Exception:
                pass
            try:
                budget.check_limits()
            except BudgetExceeded as exc:
                terminal_reason = exc.reason
                break
            # Goal accounting (Wave4 G1): add this turn + its tokens to the
            # session goal so the goal's own budgets can be applied softly.
            self._record_goal_turn(usage_input + usage_output, turn)
            # Summary handoff turn (Wave4 G1): the goal hit its per-goal budget
            # in a previous iteration and the model just wrote the handoff in
            # THIS turn (the summary prompt forbids further tool execution).
            # Take its text and end with the soft goal reason — the loop never
            # re-injects the summary prompt on a later iteration.
            if self._goal_summary_pending:
                summary_reason = self._goal_summary_reason or StopReason.GOAL_BUDGET_LIMITED
                messages.append(Message(role="assistant", content=text))
                yield {"type": "turn_complete", "turn": turn, "stop_reason": "end_turn"}
                terminal_reason = budget.finish(summary_reason)
                break
            # Goal-driven soft limit (Wave4 G1): crossing a *per-goal* budget is
            # not a hard stop.  When the goal hit its token/turn budget, record
            # the soft state and inject a summarization prompt; the loop then
            # runs ONE handoff turn in which the model writes "已达成/还差/下一步"
            # without executing further tools.  The goal survives to be resumed
            # next round (``/goal continue``).
            goal_limit = self._check_goal_limits()
            if goal_limit is not None:
                goal_store = self._resolve_goal_store()
                try:
                    state = goal_store.get(self.session_id)
                except Exception:
                    state = None
                if state is not None and not state.is_terminal():
                    if goal_limit is StopReason.GOAL_BUDGET_LIMITED:
                        try:
                            goal_store.mark_budget_limited(self.session_id)
                        except Exception:
                            pass
                    elif goal_limit is StopReason.GOAL_MAX_TURNS:
                        try:
                            goal_store.mark_max_turns(self.session_id)
                        except Exception:
                            pass
                    summary = build_budget_limited_summary_prompt(state)
                    messages.append(Message(role="user", content=summary))
                    self._goal_summary_pending = True
                    self._goal_summary_reason = goal_limit
                    continue
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
                # Wave-3 A3: record this (objective, operation, capability) into
                # the coverage matrix so the board can show what's been tried vs
                # what's left.  Best-effort; a coverage failure never breaks the
                # loop.
                try:
                    from modus.tools.coverage import mark_coverage_call

                    mark_coverage_call(
                        context.session_id, tool_name, tool_payload, result.is_error,
                    )
                except Exception:
                    pass
                if result.is_error:
                    tool_errors += 1
                else:
                    tool_successes += 1
                # Deterministic stall ledger (Wave4 G2): append this tool outcome
                # so the circuit breaker can spot a repeated failing signature.
                # Best-effort; a ledger failure never breaks the loop.
                try:
                    if self._ledger is not None:
                        self._ledger.add(
                            action=tool_name,
                            outcome="error" if result.is_error else "success",
                            error_text=model_text if result.is_error else "",
                            tokens=0,
                        )
                except Exception:
                    pass
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


# Process-wide per-session approval grant stores (Wave-3 A1/A2).
# Keyed by session_id so concurrent sub-sessions never share grants; a session
# without an id (plain CLI) shares the "default" store for the process.
_GRANT_STORES: dict[str, Any] = {}
_GRANT_LOCK: Any = None


def _grant_store(session_id: str | None) -> Any:
    """Return the SessionGrantStore for a session (creating it lazily).

    The store holds the A1 scoped approval grants and A2 rule memory for one
    session.  It is injected into every ToolContext the agent loop builds so
    the executor can consult it before asking the human.
    """
    from modus.policy.approval import SessionGrantStore

    global _GRANT_LOCK
    if _GRANT_LOCK is None:
        import threading

        _GRANT_LOCK = threading.Lock()
    key = str(session_id or "default")
    with _GRANT_LOCK:
        store = _GRANT_STORES.get(key)
        if store is None:
            store = SessionGrantStore()
            _GRANT_STORES[key] = store
        return store


# Wave5 E3: session-scoped steer / followUp queues.  The WS layer appends a
# user turn tagged ``steer:true`` to the steering queue and any other
# mid-run/end-of-run turn to the follow-up queue.  Every ReActReasoner for a
# session reads the SAME queues, so a steer queued while the run is live is
# consumed by the active reasoner before its next LLM call, and a followUp
# queued mid-run is drained into the next run's initial context.
_STEER_QUEUES: dict[str, list[Message]] = {}
_FOLLOWUP_QUEUES: dict[str, list[Message]] = {}
_QUEUE_LOCK: Any = None


def _queue_key(session_id: str | None) -> str:
    return str(session_id or "default")


def steer_queue_for(session_id: str | None) -> list[Message]:
    """Return the session-scoped steering queue (creating it lazily)."""
    global _QUEUE_LOCK
    if _QUEUE_LOCK is None:
        import threading

        _QUEUE_LOCK = threading.Lock()
    key = _queue_key(session_id)
    with _QUEUE_LOCK:
        queue = _STEER_QUEUES.get(key)
        if queue is None:
            queue = []
            _STEER_QUEUES[key] = queue
        return queue


def followup_queue_for(session_id: str | None) -> list[Message]:
    """Return the session-scoped follow-up queue (creating it lazily)."""
    global _QUEUE_LOCK
    if _QUEUE_LOCK is None:
        import threading

        _QUEUE_LOCK = threading.Lock()
    key = _queue_key(session_id)
    with _QUEUE_LOCK:
        queue = _FOLLOWUP_QUEUES.get(key)
        if queue is None:
            queue = []
            _FOLLOWUP_QUEUES[key] = queue
        return queue


def enqueue_steer(session_id: str | None, message: Message) -> None:
    """Append one user turn to a session's steering queue (thread-safe)."""
    steer_queue_for(session_id).append(message)


def enqueue_followup(session_id: str | None, message: Message) -> None:
    """Append one user turn to a session's follow-up queue (thread-safe)."""
    followup_queue_for(session_id).append(message)


def drain_followup_for(session_id: str | None) -> list[Message]:
    """Consume and return a session's pending follow-up turns."""
    queue = followup_queue_for(session_id)
    if not queue:
        return []
    consumed = list(queue)
    queue.clear()
    return consumed
