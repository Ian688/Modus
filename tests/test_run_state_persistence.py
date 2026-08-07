"""Run-state persistence: periodic budget snapshots survive a crash.

The `runs` table has a `budget` column and `update_run` supports it, but nothing
called it during a run — a power loss left the row forever ``running`` with an
empty budget.  The runner now persists a live budget snapshot every N tool
results, so an interrupted run leaves a recoverable state.
"""

from __future__ import annotations

import pytest

from modus.desktop import db


def _session() -> str:
    return db.create_session("run-state")["id"]


def _budget_snapshot() -> dict:
    return {
        "turns": 7,
        "text_chars": 1200,
        "tool_calls": 9,
        "total_tokens": 45_000,
        "limits": {"max_turns": 20, "max_tokens": 200_000, "max_wall_seconds": 600.0},
    }


def test_update_run_persists_budget_snapshot():
    """A live budget snapshot is written to the runs table."""
    sid = _session()
    run = db.create_run("run-budget-1", sid, "default")

    ok = db.update_run(run["run_id"], state="running", budget=_budget_snapshot())
    assert ok is True

    stored = db.get_run(run["run_id"])
    assert stored["state"] == "running"
    persisted = stored["budget"]  # get_run parses the JSON column already
    assert persisted["turns"] == 7
    assert persisted["tool_calls"] == 9
    assert persisted["limits"]["max_turns"] == 20


def test_run_is_recoverable_after_snapshot():
    """After a simulated crash, the last budget snapshot is readable."""
    sid = _session()
    run = db.create_run("run-budget-2", sid, "default")
    db.update_run(run["run_id"], state="running", budget=_budget_snapshot())

    # Simulate restart: read the durable state as a fresh process would.
    recovered = db.get_run(run["run_id"])
    assert recovered["state"] == "running"
    budget = recovered["budget"]
    assert budget["turns"] == 7
    # The run is interrupted, not lost: state + budget both present.
    assert recovered["started_at"] > 0
    assert recovered["ended_at"] is None


def test_terminal_update_blocks_further_snapshots():
    """Once terminal, update_run refuses to overwrite with a running state."""
    sid = _session()
    run = db.create_run("run-budget-3", sid, "default")
    db.update_run(run["run_id"], state="completed", stop_reason="completed")
    # A late periodic snapshot must not resurrect a completed run.
    ok = db.update_run(run["run_id"], state="running", budget=_budget_snapshot())
    assert ok is False
    stored = db.get_run(run["run_id"])
    assert stored["state"] == "completed"


def test_update_run_unknown_run_returns_false():
    assert db.update_run("does-not-exist", state="running") is False


def test_budget_is_redacted_before_persist():
    """Sensitive budget fields never reach the durable budget column."""
    sid = _session()
    run = db.create_run("run-budget-4", sid, "default")
    db.update_run(run["run_id"], state="running", budget={
        **_budget_snapshot(), "api_key": "sk-leak",
    })
    stored = db.get_run(run["run_id"])
    assert "sk-leak" not in stored["budget"]


def test_interrupt_preserves_live_budget_snapshot(monkeypatch, tmp_path):
    """A crash-interrupted run keeps the last periodic budget snapshot.

    This is the end-to-end power-loss path: live snapshots are written while
    running (default_runner), then interrupt_nonterminal_runs settles the row as
    interrupted/process_restart WITHOUT overwriting the budget — so a later
    restore can show how far the run got.
    """
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    sid = db.create_session("interrupt-budget")["id"]
    db.create_run("run-crash", sid, "default")
    db.update_run("run-crash", state="running", budget=_budget_snapshot())

    # Simulate Desktop restart.
    assert db.interrupt_nonterminal_runs() == 1
    recovered = db.get_run("run-crash")
    assert recovered["state"] == "interrupted"
    assert recovered["stop_reason"] == "process_restart"
    # The budget snapshot survived the interrupt.
    assert recovered["budget"]["turns"] == 7
    assert recovered["budget"]["tool_calls"] == 9
    # The run is terminal now (ended_at set).
    assert recovered["ended_at"] is not None
