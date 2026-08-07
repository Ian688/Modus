"""Board aggregation: pure, mode-independent KANBAN statistics."""

from __future__ import annotations

from modus.desktop.board_aggregation import aggregate_board, column_of_semantic_run


def _run(run_id: str, *, state: str = "completed", mode: str = "default",
         phase: str | None = None, tokens: int = 0, turns: int = 0,
         duration: float = 0.0, attention: str = "none",
         tasks: list | None = None) -> dict:
    activities = [{"sequence": 1, "phase": phase}] if phase else []
    return {
        "run_id": run_id,
        "state": state,
        "mode": mode,
        "tasks": tasks or [],
        "semantic": {
            "goal": {"summary": f"目标 {run_id}"},
            "activities": activities,
            "outcome": {"status": "completed" if state in {"completed", "failed"} else "running",
                        "attention": attention,
                        "requires_user_action": attention == "action_required"},
            "metrics": {"tokens": tokens, "turns": turns, "duration_seconds": duration},
        },
    }


def test_column_mapping_mirrors_frontend():
    assert column_of_semantic_run(_run("c1", state="completed")) == "completed"
    assert column_of_semantic_run(_run("c2", state="failed")) == "completed"
    assert column_of_semantic_run(_run("a1", state="running", phase="analyzing")) == "analyzing"
    assert column_of_semantic_run(_run("e1", state="running", phase="executing")) == "executing"
    assert column_of_semantic_run(_run("v1", state="running", phase="verifying")) == "verifying"
    # No activities -> fallback to analyzing.
    assert column_of_semantic_run({"run_id": "x", "state": "running", "semantic": {}}) == "analyzing"


def test_aggregate_board_counts_columns_and_attention():
    runs = [
        _run("done1", state="completed", tokens=100, turns=3, duration=30.0),
        _run("done2", state="completed", tokens=200, turns=5, duration=60.0),
        _run("wip1", state="running", phase="executing", tokens=50),
        _run("blocked", state="running", phase="verifying", attention="blocked"),
        _run("needs", state="running", phase="analyzing", attention="action_required"),
    ]
    board = aggregate_board(runs)

    assert board["columns"]["completed"]["count"] == 2
    assert board["columns"]["executing"]["count"] == 1
    assert board["columns"]["verifying"]["count"] == 1
    assert board["columns"]["analyzing"]["count"] == 1
    assert board["summary"]["total_runs"] == 5
    assert board["summary"]["in_progress"] == 3
    assert board["summary"]["completion_rate"] == 0.4
    assert board["summary"]["total_tokens"] == 350
    assert board["summary"]["total_turns"] == 8
    # Cycle time averages over completed runs only.
    assert board["summary"]["cycle_time_avg_seconds"] == 45.0
    assert [r["run_id"] for r in board["summary"]["blocked"]] == ["blocked"]
    assert [r["run_id"] for r in board["summary"]["needs_action"]] == ["needs"]


def test_aggregate_board_is_mode_independent():
    runs = [
        _run("d", state="completed", mode="default"),
        _run("m", state="running", mode="moa", phase="executing"),
        _run("p", state="running", mode="peri", phase="analyzing"),
        _run("a", state="completed", mode="agi"),
    ]
    board = aggregate_board(runs)

    assert board["modes"] == {"agi": 1, "default": 1, "moa": 1, "peri": 1}


def test_aggregate_board_counts_workers_from_tasks():
    runs = [
        _run("host", state="completed", tasks=[
            {"task_kind": "root"}, {"task_kind": "worker"}, {"task_kind": "worker"},
        ]),
    ]
    board = aggregate_board(runs)

    assert board["worker_count"] == 2


def test_aggregate_board_total_on_malformed_input():
    board = aggregate_board([None, {}, {"run_id": "ok", "state": "completed", "semantic": {}}])

    # None is filtered out (not a dict); {} falls to analyzing, ok is completed.
    assert board["summary"]["total_runs"] == 2
    assert board["summary"]["completed"] == 1
    assert board["columns"]["analyzing"]["count"] == 1
    assert board["summary"]["in_progress"] == 1
    # No division by zero.
    assert board["summary"]["completion_rate"] >= 0
