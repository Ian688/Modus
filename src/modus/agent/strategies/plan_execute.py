"""Plan-and-Execute reasoning strategy.

Decomposes a complex goal into a dependency-ordered task plan (via one LLM
call), then executes each task with a normal tool-use loop.  Emits the exact
same event vocabulary as ``ReActReasoner`` so runners and the frontend consume
both strategies unchanged.  When planning fails (malformed JSON, dependency
cycle) it degrades gracefully to plain ReAct over the whole request.

The plan/execute split is structural, not a new tool: each task runs an inner
ReAct loop with a task-focused prompt and the shared tool registry, budget and
approval callback, so every safety boundary (HITL, CommandGuard, PathGuard,
verification) applies identically.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from modus.agent.planning import ExecutionPlan, parse_plan
from modus.config import ModusConfig
from modus.llm.base import LlmClient
from modus.runtime.budget import RunBudget, RunLimits, StopReason
from modus.tools.registry import ToolRegistry
from modus.types import Message

_PLANNER_PROMPT = (
    "You are a planning agent. Given the user's goal, produce a JSON execution "
    "plan decomposing it into 2-6 ordered tasks. Respond with ONLY a JSON "
    "object (no markdown fences, no prose):\n"
    "{\"summary\": \"one-line plan summary\", \"tasks\": [\n"
    "  {\"id\": \"task_1\", \"description\": \"what to do\", "
    "\"type\": \"file_read|file_write|command|analysis|verification\", "
    "\"dependencies\": []},\n"
    "  {\"id\": \"task_2\", \"description\": \"depends on task_1\", "
    "\"type\": \"command\", \"dependencies\": [\"task_1\"]}\n"
    "]}\n"
    "Rules: keep tasks concrete and executable with tools; every dependency "
    "must reference an existing task id; do not include cycles; a simple "
    "single-step goal may use one task."
)


class PlanExecuteReasoner:
    """Plan-then-execute: decompose, order by dependency, run each task."""

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
        max_tasks: int = 8,
        fallback_to_react: bool = True,
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
        self.max_tasks = max_tasks
        self.fallback_to_react = fallback_to_react
        # Per-task outcome ledger for replan-on-failure.
        self._task_outcomes: dict[str, str] = {}
        self._replanned = False

    def _task_budget(self) -> RunBudget:
        """Derive a per-task budget from the shared run budget's limits."""
        limits = self.budget.limits
        # A task consumes at most half the run's turns and a share of tokens.
        task_limits = RunLimits(
            max_turns=max(1, limits.max_turns // 2),
            max_tokens=max(1_000, limits.max_tokens // max(1, self.max_tasks)),
            max_wall_seconds=limits.max_wall_seconds,
            max_verification_attempts=limits.max_verification_attempts,
        )
        return RunBudget(task_limits)

    def _run_react(self, messages, approval_callback, cancel_event, *, budget=None):
        from modus.agent.strategies import ReActReasoner

        reasoner = ReActReasoner(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            system_prompt=self.system_prompt,
            cwd=self.cwd,
            config=self.config,
            max_turns=self.max_turns,
            budget=budget or self.budget,
            session_id=self.session_id,
            run_id=self.run_id,
        )
        return reasoner.run(messages, approval_callback=approval_callback,
                            cancel_event=cancel_event)

    async def run(
        self,
        messages: list[Message],
        *,
        approval_callback: Callable[[dict[str, Any]], Awaitable[str] | str] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        goal = _latest_user_text(messages)
        plan = None
        if goal:
            plan = await self._make_plan(goal)
            if plan is not None and len(plan.tasks) > self.max_tasks:
                plan = None

        if plan is None and self.fallback_to_react:
            # Graceful degradation: treat the whole request as one ReAct task.
            async for event in self._run_react(messages, approval_callback, cancel_event):
                yield event
            return
        if plan is None:
            yield {
                "type": "done", "total_turns": self.budget.turns,
                "total_tokens": self.budget.total_tokens, "messages": messages,
                "stop_reason": StopReason.FAILED.value,
                "budget": self.budget.snapshot(),
                "verification": self.budget.verification.snapshot(),
            }
            return

        yield {"type": "plan", "goal": plan.goal, "summary": plan.summary,
               "tasks": [task.id for task in plan.tasks]}
        completed: set[str] = set()
        task_outputs: dict[str, str] = {}
        terminal_reason = StopReason.COMPLETED
        messages_snapshot = list(messages)

        while True:
            if cancel_event is not None and cancel_event.is_set():
                terminal_reason = StopReason.CANCELLED
                break
            ready = plan.ready_tasks(completed)
            if not ready:
                break
            if self.budget.turns >= self.budget.limits.max_turns:
                terminal_reason = StopReason.MAX_TURNS
                break
            # Run the ready batch: parallelizable tasks (read/analysis/
            # verification) run concurrently, serial tasks (write/command)
            # run one at a time.
            async for event in self._run_task_batch(
                ready, plan, task_outputs, messages_snapshot,
                approval_callback, cancel_event,
            ):
                yield event
            if terminal_reason in {StopReason.MAX_TURNS, StopReason.CANCELLED}:
                break
            for task in ready:
                completed.add(task.id)
            # Replan-on-failure: if a task in this batch ended non-completed,
            # rebuild the remaining plan once and continue.  Gated off while
            # verification is required (the verification loop owns recovery).
            failed = [
                task for task in ready
                if self._task_outcomes.get(task.id, "completed") != "completed"
            ]
            verification_required = bool(
                self.budget.verification.snapshot().get("required", False)
            )
            if failed and not self._replanned and not verification_required:
                self._replanned = True
                failed_desc = "\n".join(
                    f"- {task.id}: {task.description}" for task in failed
                )
                replan_goal = (
                    f"{plan.goal}\n\n已完成任务："
                    f"{', '.join(sorted(completed - {f.id for f in failed}))}\n"
                    f"失败任务需重新规划：\n{failed_desc}"
                )
                new_plan = await self._make_plan(replan_goal)
                if new_plan is not None:
                    yield {"type": "plan_replan",
                           "failed_task_ids": [task.id for task in failed],
                           "revised_tasks": [task.id for task in new_plan.tasks]}
                    # The failed tasks were marked completed above; unmark them
                    # so the new plan re-runs them.
                    for task in failed:
                        completed.discard(task.id)
                    plan = new_plan
                    continue
            if not plan.ready_tasks(completed):
                break

        all_done = len(completed) == len(plan.tasks)
        if terminal_reason is StopReason.COMPLETED and not all_done:
            terminal_reason = StopReason.FAILED
        self.budget.finish(terminal_reason)
        snapshot = self.budget.snapshot()
        snapshot["verification"] = self.budget.verification.snapshot()
        yield {
            "type": "done", "total_turns": self.budget.turns,
            "total_tokens": self.budget.total_tokens,
            "messages": messages_snapshot, "stop_reason": terminal_reason.value,
            "budget": snapshot, "verification": snapshot["verification"],
            "plan": {"goal": plan.goal, "summary": plan.summary,
                     "tasks": [t.id for t in plan.tasks], "completed": sorted(completed)},
        }

    async def _make_plan(self, goal: str) -> ExecutionPlan | None:
        """Ask the LLM for a plan JSON; parse it (best-effort)."""
        try:
            text = ""
            async for event in self.llm_client.chat(
                [Message(role="user", content=f"Goal: {goal}\n\n{_PLANNER_PROMPT}")],
                [], system_prompt=self.system_prompt,
            ):
                if event.get("type") == "text_delta":
                    text += str(event.get("text") or "")
            return parse_plan(goal, text)
        except Exception:
            return None

    def _execute_task_stream(
        self, plan, task, task_outputs, messages_snapshot,
        approval_callback, cancel_event,
    ):
        """Run one task's inner ReAct loop, yielding its events.

        Yields the task's events (text/tool/error/done from the inner loop)
        and, as a side channel, collects the final text via the generator's
        ``send`` value is not used; the caller reads ``task_outputs`` after.
        """
        async def stream():
            task_context = _task_messages(plan, task, task_outputs, messages_snapshot)
            # Record the pre-run context length.  ReActReasoner appends its
            # assistant/tool messages IN PLACE to ``task_context``, so the
            # post-run list is a superset of the pre-run context; the new tail
            # is what belongs to this task and must merge back into the shared
            # run context so ``done.messages`` reflects full history.
            pre_context_len = len(task_context)
            text = ""
            # Each task gets its own stall window so a no-progress tail in one
            # task cannot kill the next.  Tokens/turns are merged back into the
            # shared budget after the task.
            task_budget = self._task_budget()
            task_stop = "completed"
            task_done_messages: list[Message] | None = None
            async for event in self._run_react(
                task_context, approval_callback, cancel_event, budget=task_budget,
            ):
                if event.get("type") == "done":
                    task_stop = str(event.get("stop_reason") or "completed")
                    task_done_messages = list(event.get("messages") or [])
                elif event.get("type") == "error":
                    task_stop = str(event.get("stop_reason") or "engine_error")
                if event.get("type") == "text_delta":
                    text += str(event.get("text") or "")
                yield event
            self._task_outcomes[task.id] = task_stop
            self.budget.record_usage(
                task_budget.input_tokens, task_budget.output_tokens,
                owner=f"plan:task_{task.id}",
            )
            self.budget.turns = min(
                self.budget.limits.max_turns,
                self.budget.turns + task_budget.turns,
            )
            # Merge this task's new messages back into the shared run context so
            # ``done.messages`` reflects the full plan-execute history (the
            # desktop runner and Agent.history depend on it).  ``task_context``
            # is mutated in place by the inner loop; its tail beyond the pre-run
            # length holds this task's assistant/tool turns.
            if task_done_messages is not None and len(task_done_messages) > pre_context_len:
                messages_snapshot.extend(task_done_messages[pre_context_len:])
        return stream()

    def _task_is_parallelizable(self, task: Any) -> bool:
        """Read/analysis/verification tasks are safe to run concurrently.

        Write and command tasks mutate shared state, so they stay serial.
        """
        return task.kind in {"file_read", "analysis", "verification"}

    async def _run_task_batch(
        self, tasks, plan, task_outputs, messages_snapshot,
        approval_callback, cancel_event,
    ):
        """Yield a ready batch's events, parallelizing the safe tasks.

        Parallelizable tasks (read/analysis/verification) run concurrently via
        gather; serial tasks (write/command) run one at a time.  Each task's
        final text is written into ``task_outputs`` as it finishes.
        """
        parallel = [task for task in tasks if self._task_is_parallelizable(task)]
        serial = [task for task in tasks if not self._task_is_parallelizable(task)]

        async def run_one(task):
            """Run one task, collecting its events; returns (task_id, events)."""
            events: list[dict[str, Any]] = []
            text = ""
            async for event in self._execute_task_stream(
                plan, task, task_outputs, messages_snapshot,
                approval_callback, cancel_event,
            ):
                events.append(event)
                if event.get("type") == "text_delta":
                    text += str(event.get("text") or "")
            task_outputs[task.id] = text
            return task.id, events

        # Parallelizable tasks run concurrently.
        if parallel:
            collected = await asyncio.gather(
                *(run_one(task) for task in parallel), return_exceptions=True,
            )
            for task, outcome in zip(parallel, collected):
                if isinstance(outcome, Exception):
                    task_outputs[task.id] = f"[任务执行异常: {outcome}]"
                    continue
                _tid, events = outcome
                yield {"type": "task_start", "task_id": task.id,
                       "description": task.description}
                for event in events:
                    yield event
                yield {"type": "task_complete", "task_id": task.id,
                       "result": str(task_outputs.get(task.id, ""))[:200]}

        # Serial tasks run one at a time.
        for task in serial:
            yield {"type": "task_start", "task_id": task.id,
                   "description": task.description}
            async for event in self._execute_task_stream(
                plan, task, task_outputs, messages_snapshot,
                approval_callback, cancel_event,
            ):
                yield event
            yield {"type": "task_complete", "task_id": task.id,
                   "result": str(task_outputs.get(task.id, ""))[:200]}


def _latest_user_text(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.role == "user" and isinstance(message.content, str):
            return message.content
    return ""


def _task_messages(
    plan: ExecutionPlan, task: Any, outputs: dict[str, str],
    original: list[Message],
) -> list[Message]:
    """Build the model context for one task: goal + dependency results + task."""
    by_id = {t.id: t for t in plan.tasks}
    deps_context: list[str] = []
    for dep_id in task.dependencies:
        dep = by_id.get(dep_id)
        label = dep.description if dep else dep_id
        result = outputs.get(dep_id, "")
        deps_context.append(f"- [{dep_id}] {label}\n  result: {result[:400]}")
    dep_block = "\n".join(deps_context) if deps_context else "- 无依赖"
    task_prompt = (
        f"总目标：{plan.goal}\n\n"
        f"当前任务 [{task.id}]：{task.description}\n\n"
        f"依赖任务结果：\n{dep_block}\n\n"
        f"请完成此任务。用工具验证你的结论。完成时给出简洁结果。"
    )
    # Keep the system contract, then the task prompt as the user turn.
    head = [m for m in original if m.role == "system"][:1]
    return [*head, Message(role="user", content=task_prompt)]
