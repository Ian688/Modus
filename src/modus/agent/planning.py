"""Plan-and-Execute planning data structures and parsing.

``PlanExecuteReasoner`` (in ``strategies/plan_execute.py``) decomposes a
complex goal into a dependency-ordered task plan, then executes each task with
a normal tool-use loop.  This module owns the pure plan model and the
deterministic parser for the LLM's JSON plan output, so the plan shape is
testable without a model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PlanTask:
    id: str
    description: str
    kind: str = "analysis"  # analysis / command / file_read / file_write / verification
    dependencies: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExecutionPlan:
    goal: str
    summary: str = ""
    tasks: list[PlanTask] = field(default_factory=list)
    _order: list[str] = field(default_factory=list)

    def compute_order(self) -> bool:
        """Topological sort; returns False on a dependency cycle."""
        by_id = {task.id: task for task in self.tasks}
        indegree: dict[str, int] = {task.id: 0 for task in self.tasks}
        dependents: dict[str, list[str]] = {task.id: [] for task in self.tasks}
        for task in self.tasks:
            for dep in task.dependencies:
                if dep not in by_id:
                    # Unknown dependency is treated as a missing prerequisite:
                    # fail the whole plan rather than guess.
                    return False
                indegree[task.id] += 1
                dependents[dep].append(task.id)
        ready = [tid for tid, degree in indegree.items() if degree == 0]
        order: list[str] = []
        while ready:
            tid = ready.pop(0)
            order.append(tid)
            for child in dependents[tid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if len(order) != len(self.tasks):
            return False
        self._order = order
        return True

    @property
    def execution_order(self) -> list[str]:
        return list(self._order)

    def ready_tasks(self, completed: set[str]) -> list[PlanTask]:
        """Return tasks whose dependencies are all completed, in order."""
        by_id = {task.id: task for task in self.tasks}
        return [
            task for task in self.tasks
            if task.id not in completed
            and all(dep in completed for dep in task.dependencies)
        ]


# Matches ```json ... ``` fences and bare JSON objects.
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_plan(goal: str, text: str) -> ExecutionPlan | None:
    """Parse the LLM's plan JSON into an ExecutionPlan, or None on failure.

    Tolerates markdown fences and stray prose around the JSON object.  A
    dependency cycle or malformed task array yields None (the caller falls
    back to a single-task plan or plain ReAct).
    """
    if not text or not text.strip():
        return None
    candidate = text.strip()
    fenced = _JSON_FENCE_RE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    else:
        obj = _JSON_OBJECT_RE.search(candidate)
        if obj:
            candidate = obj.group(0)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    tasks_raw = data.get("tasks")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        return None
    plan = ExecutionPlan(
        goal=goal,
        summary=str(data.get("summary") or "")[:300],
    )
    for index, raw in enumerate(tasks_raw):
        if not isinstance(raw, dict):
            continue
        description = str(raw.get("description") or "").strip()
        if not description:
            continue
        deps = [
            str(item) for item in (raw.get("dependencies") or [])
            if isinstance(item, str) and item.strip()
        ]
        plan.tasks.append(PlanTask(
            id=str(raw.get("id") or f"task_{index + 1}"),
            description=description[:500],
            kind=str(raw.get("type") or "analysis").strip().lower(),
            dependencies=deps,
        ))
    if not plan.tasks:
        return None
    if not plan.compute_order():
        return None
    return plan
