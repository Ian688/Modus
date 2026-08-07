"""Read-only board aggregation over semantic run projections (KANBAN base).

The Workbench already projects every run into ``modus.semantic-run.v1``.  This
module derives the board-level statistics the frontend column headers and
summary strip want — per-column counts, work-in-progress, cycle time, blocked
runs, mode distribution — as pure functions over that projection.  No database
schema, no event vocabulary, no mode branching: default / MOA / Peri / AGI all
feed the same aggregate.

Every function here is total and deterministic: a malformed or partial run
contributes zero rather than raising, so the board never breaks on incomplete
data.
"""

from __future__ import annotations

from typing import Any

# Column keys mirror kanban.js COLUMNS so the frontend can address them directly.
COLUMN_KEYS = ("todo", "analyzing", "executing", "verifying", "completed")

# Phase -> column mapping, matching kanban.js COLUMN_OF_PHASE.
_PHASE_TO_COLUMN = {
    "analyzing": "analyzing",
    "executing": "executing",
    "verifying": "verifying",
    "approving": "analyzing",
    "reviewing": "executing",
    "delivering": "executing",
    "completed": "completed",
    "failed": "completed",
}


def column_of_semantic_run(run: dict[str, Any]) -> str:
    """Return the board column a semantic run belongs to.

    Mirrors ``kanban.js`` ``columnOfRun``: a terminal state pins the run to
    ``completed``; otherwise the latest activity phase picks the in-progress
    column.  Unknown/absent data falls back to ``analyzing``.
    """
    state = str((run or {}).get("state") or "")
    if state in {"completed", "failed", "cancelled", "interrupted"}:
        return "completed"
    semantic = (run or {}).get("semantic") or {}
    activities = semantic.get("activities") or []
    latest_phase = ""
    latest_sequence = -1
    for activity in activities:
        sequence = _integer(activity.get("sequence"))
        if sequence >= latest_sequence:
            latest_sequence = sequence
            phase = str(activity.get("phase") or "")
            if phase:
                latest_phase = phase
    return _PHASE_TO_COLUMN.get(latest_phase, "analyzing")


def _column_counts(runs: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in COLUMN_KEYS}
    for run in runs:
        column = column_of_semantic_run(run)
        counts[column] = counts.get(column, 0) + 1
    return counts


def _blocked_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocked = []
    for run in runs:
        outcome = ((run or {}).get("semantic") or {}).get("outcome") or {}
        if str(outcome.get("attention") or "") == "blocked":
            blocked.append(_run_ref(run))
    return blocked


def _needs_action_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    waiting = []
    for run in runs:
        outcome = ((run or {}).get("semantic") or {}).get("outcome") or {}
        if outcome.get("requires_user_action") is True or str(
            outcome.get("attention") or ""
        ) == "action_required":
            waiting.append(_run_ref(run))
    return waiting


def _cycle_time_seconds(run: dict[str, Any]) -> float:
    """Wall seconds between run start and terminal outcome, else 0."""
    semantic = (run or {}).get("semantic") or {}
    metrics = semantic.get("metrics") or {}
    duration = _number(metrics.get("duration_seconds"))
    if duration <= 0:
        return 0.0
    outcome = semantic.get("outcome") or {}
    if str(outcome.get("status") or "") == "running":
        return 0.0
    return round(duration, 1)


def _run_ref(run: dict[str, Any]) -> dict[str, Any]:
    semantic = (run or {}).get("semantic") or {}
    goal = semantic.get("goal") or {}
    return {
        "run_id": str((run or {}).get("run_id") or ""),
        "mode": str((run or {}).get("mode") or "default"),
        "state": str((run or {}).get("state") or "running"),
        "goal": str(goal.get("summary") or "")[:120],
    }


def aggregate_board(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate semantic runs into one board-level DTO.

    Pure and deterministic: the input is exactly the Workbench run list (each
    with its ``semantic`` projection).  Output is keyed for the frontend
    board columns plus a summary strip; every field has a safe default.
    """
    runs = [run for run in runs if isinstance(run, dict)]
    counts = _column_counts(runs)
    in_progress = sum(counts[key] for key in ("analyzing", "executing", "verifying"))
    completed_count = counts["completed"]

    # Cycle time is measured on terminal completed runs only.
    completed_runs = [run for run in runs if column_of_semantic_run(run) == "completed"]
    cycle_times = [_cycle_time_seconds(run) for run in completed_runs]
    cycles = [ct for ct in cycle_times if ct > 0]

    total_tokens = 0
    total_turns = 0
    for run in runs:
        metrics = ((run or {}).get("semantic") or {}).get("metrics") or {}
        total_tokens += _integer(metrics.get("tokens"))
        total_turns += _integer(metrics.get("turns"))

    modes: dict[str, int] = {}
    for run in runs:
        mode = str((run or {}).get("mode") or "default")
        modes[mode] = modes.get(mode, 0) + 1

    worker_count = sum(
        1
        for run in runs
        for task in ((run or {}).get("tasks") or [])
        if str(task.get("task_kind") or "") != "root"
    )

    return {
        "schema": "modus.board-aggregation.v1",
        "columns": {
            key: {
                "key": key,
                "count": counts[key],
                # Runs with an active human attention need (blocked or
                # action_required) in this column.
                "attention": sum(
                    1
                    for run in runs
                    if column_of_semantic_run(run) == key
                    and str(
                        (((run or {}).get("semantic") or {}).get("outcome") or {}).get("attention")
                        or ""
                    )
                    in {"blocked", "action_required"}
                ),
            }
            for key in COLUMN_KEYS
        },
        "summary": {
            "total_runs": len(runs),
            "completed": completed_count,
            "in_progress": in_progress,
            "wip": in_progress,
            "completion_rate": round(completed_count / len(runs), 3) if runs else 0.0,
            "cycle_time_avg_seconds": round(sum(cycles) / len(cycles), 1) if cycles else 0.0,
            "total_tokens": total_tokens,
            "total_turns": total_turns,
            "blocked": [_run_ref(run) for run in _blocked_runs(runs)],
            "needs_action": _needs_action_runs(runs),
        },
        "modes": dict(sorted(modes.items())),
        "worker_count": worker_count,
    }


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _number(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0
