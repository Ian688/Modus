"""Wave5 E1: trajectory sink → offline Evaluator join → static-json scoring → EvalReport.

Covers:
- run_events carry an evaluable ``tool_calls`` summary (name/input/output + sha256);
- ``create_run``/``create_run_admission`` persist the ``objective``;
- ``settle_run_event`` writes ``final_result`` and sinks ``{run_id}.json`` with
  tool_calls (dataclass-aware serialization survives a ToolResult payload);
- ``Evaluator`` joins scenario × trajectory via contextvar injection and the
  scorer registry (register/get/names);
- static_json determinism: numeric tolerance / interval / delta-1 / precision
  /recall/F1;
- the CLI ``modus evaluate --run`` and ``--suite`` paths produce a report.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from modus.desktop import db


# ── fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def eval_db(tmp_path, monkeypatch):
    """Isolate desktop.db onto a temp data dir and reset runtime flags."""
    from modus.desktop import db as db_module

    db_module.release_writer_lease()
    monkeypatch.setattr(db_module, "DB_DIR", tmp_path)
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "desktop.db")
    monkeypatch.setattr(db_module, "_integrity_checked", False)
    monkeypatch.setattr(db_module, "_recovering", False)
    monkeypatch.setattr(db_module, "_checkpoint_calls", 0)
    db_module.init_db()
    return db_module


def _admit_run(database, *, run_id: str, session_id: str, objective: str = "") -> str:
    """Admit a durable run with a root task (create_run_admission)."""
    admitted = database.create_run_admission(
        run_id, session_id, "default",
        objective=objective,
        config_snapshot={"schema": "modus.run-config.v1", "host_model_id": "model-a"},
    )
    assert admitted.get("run_id") == run_id, "run admission must succeed"
    return run_id


def _settle_completed(database, *, run_id: str, session_id: str,
                      sequence: int = 10, budget: dict | None = None,
                      tool_calls: list | None = None) -> None:
    """Persist a tool_call + host_response and settle the run as completed."""
    database.upsert_run_event(session_id, {
        "event_id": f"evt-call-{run_id}", "run_id": run_id,
        "channel_id": "user_host", "parent_event_id": None, "sequence": sequence,
        "timestamp": "2026-08-08T00:00:00Z", "mode": "default",
        "actor": {"kind": "tool", "id": "calc", "label": "calculate"},
        "type": "tool_call", "status": "completed",
        "payload": {"tool_call_id": "tc1", "name": "calculate",
                    "input": {"a": 60, "b": 40}, "result": "100"},
    })
    database.upsert_run_event(session_id, {
        "event_id": f"evt-resp-{run_id}", "run_id": run_id,
        "channel_id": "user_host", "parent_event_id": None, "sequence": sequence + 1,
        "timestamp": "2026-08-08T00:00:00Z", "mode": "default",
        "actor": {"kind": "host", "id": "primary", "label": "主持人"},
        "type": "host_response", "status": "completed",
        "payload": {"markdown": 'The total is ```json\n{"total": 100}\n```'},
    })
    terminal = {
        "event_id": f"evt-term-{run_id}", "run_id": run_id,
        "channel_id": "user_host", "parent_event_id": None, "sequence": sequence + 2,
        "timestamp": "2026-08-08T00:00:01Z", "mode": "default",
        "actor": {"kind": "host", "id": "primary", "label": "主持人"},
        "type": "run_completed", "status": "completed",
        "payload": {"stop_reason": "completed",
                    "budget": budget or {"total_tokens": 100, "input_tokens": 40,
                                         "output_tokens": 60}},
        "task_id": f"task_{run_id}_root",
    }
    if tool_calls is not None:
        terminal["payload"]["tool_calls"] = tool_calls
    assert database.settle_run_event(session_id, terminal) is True


# ── trajectory sink ──────────────────────────────────────────────────────

def test_run_events_carry_evaluable_tool_calls(eval_db):
    database = eval_db
    session = database.create_session("traj toolcalls")
    run_id = _admit_run(database, run_id="run-tc", session_id=session["id"])
    database.upsert_run_event(session["id"], {
        "event_id": "evt-call", "run_id": run_id, "channel_id": "user_host",
        "parent_event_id": None, "sequence": 1, "timestamp": "now",
        "mode": "default", "actor": {"kind": "tool", "id": "calc", "label": "calculate"},
        "type": "tool_call", "status": "completed",
        "payload": {"tool_call_id": "tc1", "name": "calculate",
                    "input": {"a": 60, "b": 40}, "result": "100"},
    })
    events = database.get_run_events(run_id)
    assert len(events) == 1
    calls = events[0]["tool_calls"]
    assert isinstance(calls, list) and len(calls) == 1
    summary = calls[0]
    assert summary["name"] == "calculate"
    assert "a" in summary["input_summary"]
    assert "100" in summary["output_summary"]
    assert len(summary["sha256"]) == 64  # sha256 hex digest


def test_create_run_persists_objective(eval_db):
    database = eval_db
    session = database.create_session("objective")
    database.create_run("run-obj", session["id"], "default", objective="add two numbers")
    run = database.get_run("run-obj")
    assert run["objective"] == "add two numbers"
    # create_run_admission also persists it
    run_id = _admit_run(database, run_id="run-obj2", session_id=session["id"],
                        objective="explain the answer")
    assert database.get_run(run_id)["objective"] == "explain the answer"


def test_settle_run_event_writes_final_result_and_sinks_trajectory(eval_db):
    database = eval_db
    session = database.create_session("settle traj")
    run_id = _admit_run(database, run_id="run-final", session_id=session["id"],
                        objective="compute total")
    _settle_completed(database, run_id=run_id, session_id=session["id"])
    run = database.get_run(run_id)
    assert run["state"] == "completed"
    assert run["final_result"] != ""
    assert "100" in run["final_result"]

    # The trajectory file exists under ~/.modus/trajectories with tool_calls.
    path = database._trajectory_file(run_id)
    assert path.exists()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["schema"] == "modus.trajectory.v1"
    assert doc["run_id"] == run_id
    assert doc["objective"] == "compute total"
    assert doc["final_result"] == run["final_result"]
    assert doc["state"] == "completed"
    events = doc["events"]
    assert any(event["type"] == "tool_call" and event["tool_calls"] for event in events)


def test_persist_trajectory_roundtrips_load_and_list(eval_db):
    database = eval_db
    session = database.create_session("roundtrip")
    run_id = _admit_run(database, run_id="run-rt", session_id=session["id"])
    _settle_completed(database, run_id=run_id, session_id=session["id"])

    doc = database.load_trajectory(run_id)
    assert doc is not None
    assert doc["run_id"] == run_id
    assert len(doc["events"]) >= 3  # tool_call + host_response + terminal

    listing = database.list_trajectories()
    assert any(item["run_id"] == run_id for item in listing)


def test_persist_trajectory_survives_dataclass_payload(eval_db):
    """persist_trajectory serializes dataclass-bearing payloads via its default."""
    from dataclasses import dataclass

    database = eval_db
    session = database.create_session("dataclass")
    run_id = _admit_run(database, run_id="run-dc", session_id=session["id"])

    @dataclass
    class _ToolResult:
        value: int
        ok: bool

    # The run_events payload is already JSON by the time it reaches SQLite, so
    # dataclass-aware serialization matters at the trajectory sink, not the
    # ledger insert.  Hand a dataclass-bearing event directly to the sink.
    events = [{
        "sequence": 1, "event_id": "evt-dc", "type": "tool_result",
        "status": "completed", "timestamp": "now",
        "actor": {"kind": "tool", "id": "t", "label": "t"},
        "tool_calls": [],
        "payload": {"tool_call_id": "tc", "name": "t", "result": _ToolResult(value=42, ok=True)},
    }]
    run = database.get_run(run_id)
    path = database.persist_trajectory(run_id, run=run, events=events)
    assert path is not None and path.exists()
    doc = database.load_trajectory(run_id)
    assert doc is not None
    assert doc["events"][0]["payload"]["result"]["value"] == 42
    assert doc["events"][0]["payload"]["result"]["ok"] is True


def test_interrupt_nonterminal_run_sinks_trajectory(eval_db):
    database = eval_db
    session = database.create_session("interrupt")
    run_id = _admit_run(database, run_id="run-interrupt", session_id=session["id"])
    database.upsert_run_event(session["id"], {
        "event_id": "evt-x", "run_id": run_id, "channel_id": "user_host",
        "parent_event_id": None, "sequence": 1, "timestamp": "now",
        "mode": "default", "actor": {"kind": "host", "id": "primary", "label": "主持人"},
        "type": "host_response", "status": "completed",
        "payload": {"markdown": "work in progress"},
    })
    assert database.interrupt_nonterminal_runs() == 1
    run = database.get_run(run_id)
    assert run["state"] == "interrupted"
    assert run["final_result"] != ""  # process_restart message is the final_result
    doc = database.load_trajectory(run_id)
    assert doc is not None and doc["state"] == "interrupted"


def test_trajectory_objective_falls_back_to_user_message(eval_db):
    """A run admitted without an objective still sinks a self-describing one."""
    database = eval_db
    session = database.create_session("obj fallback")
    run_id = _admit_run(database, run_id="run-no-obj", session_id=session["id"])
    database.upsert_run_event(session["id"], {
        "event_id": "evt-u", "run_id": run_id, "channel_id": "user_host",
        "parent_event_id": None, "sequence": 1, "timestamp": "now",
        "mode": "default", "actor": {"kind": "user", "id": "user", "label": "用户"},
        "type": "user_message", "status": "completed",
        "payload": {"markdown": "calculate the total price"},
    })
    _settle_completed(database, run_id=run_id, session_id=session["id"], sequence=2)
    doc = database.load_trajectory(run_id)
    assert doc is not None
    assert doc["objective"] == "calculate the total price"


# ── Evaluator + scorer registry ──────────────────────────────────────────

def _scenario(*, scenario_id: str, expected, run_id: str = "", **match):
    """Build a scenario dict with run_id / judge_model at top level."""
    scenario = {"scenario_id": scenario_id, "expected": expected}
    if run_id:
        scenario["run_id"] = run_id
    if match:
        scenario["match"] = match
    return scenario


def test_evaluator_joins_scenario_and_trajectory(eval_db):
    from modus.evaluation import Evaluator, scorer_names, get_scorer, register_scorer
    from modus.evaluation.evaluator import EvaluationError

    database = eval_db
    session = database.create_session("join")
    run_id = _admit_run(database, run_id="run-join", session_id=session["id"])
    _settle_completed(database, run_id=run_id, session_id=session["id"])

    evaluator = Evaluator()
    assert "static_json" in scorer_names()

    # Score by run_id string (resolved from the ledger) and by trajectory dict.
    for trajectory in (run_id, database.load_trajectory(run_id)):
        score = evaluator.score(_scenario(scenario_id="s1", expected={"total": 100}), trajectory)
        assert score["pass"] is True
        assert score["scorer"] == "static_json"
        assert score["run_id"] == run_id
        assert score["scenario_id"] == "s1"

    report = evaluator.evaluate([
        (_scenario(scenario_id="s1", expected={"total": 100}), run_id),
    ])
    assert report["summary"]["pass"] is True
    assert report["summary"]["passed"] == 1

    # Registry get/names work; unknown scorer fails loudly.
    assert callable(get_scorer("static_json"))
    with pytest.raises(EvaluationError):
        get_scorer("no-such-scorer")

    def _custom(scenario, trajectory, **opts):  # deterministic pure fn
        return {"pass": True, "partial": True, "reason": "custom"}

    register_scorer("custom", _custom)
    assert "custom" in scorer_names()
    assert evaluator.score(_scenario(scenario_id="s2", expected={}), run_id, scorer="custom")["pass"] is True


def test_evaluator_binds_contextvars_per_join(eval_db):
    from modus.evaluation import Evaluator, active_run_id, active_scenario_id

    database = eval_db
    session = database.create_session("ctx")
    run_a = _admit_run(database, run_id="run-ctx-a", session_id=session["id"])
    run_b = _admit_run(database, run_id="run-ctx-b", session_id=session["id"])
    _settle_completed(database, run_id=run_a, session_id=session["id"])
    _settle_completed(database, run_id=run_b, session_id=session["id"])

    captured: list[tuple] = []

    def _ctx_scorer(scenario, trajectory, **opts):
        captured.append((active_run_id(), active_scenario_id()))
        return {"pass": True, "partial": True, "reason": "ctx"}

    from modus.evaluation import register_scorer

    register_scorer("ctx", _ctx_scorer)
    evaluator = Evaluator()
    evaluator.evaluate([
        (_scenario(scenario_id="ctx-a", expected={}, run_id=run_a), run_a),
        (_scenario(scenario_id="ctx-b", expected={}, run_id=run_b), run_b),
    ], scorer="ctx")
    assert sorted(captured) == sorted([(run_a, "ctx-a"), (run_b, "ctx-b")])


def test_llm_judge_self_review_guard():
    """A judge sharing the trajectory's model must be refused (self-review guard)."""
    from modus.evaluation import Evaluator, register_scorer
    from modus.evaluation.evaluator import EvaluationError

    def _judge(scenario, trajectory, **opts):
        return {"pass": True, "partial": True, "reason": "judged"}

    register_scorer("llm_judge", _judge)
    evaluator = Evaluator()
    scenario = {"scenario_id": "g1", "expected": {}, "judge_model": "model-a"}
    # Same model as the trajectory → guard refuses.
    with pytest.raises(EvaluationError, match="self-review"):
        evaluator.score(scenario, {"run_id": "r", "model_id": "model-a"}, scorer="llm_judge")
    # A different judge model is allowed.
    assert evaluator.score(
        {"scenario_id": "g2", "expected": {}, "judge_model": "model-b"},
        {"run_id": "r", "model_id": "model-a"}, scorer="llm_judge",
    )["pass"] is True
    # A scenario without judge_model (any deterministic scorer) is unaffected.
    assert evaluator.score(
        {"scenario_id": "g3", "expected": {"x": 1}},
        {"run_id": "r", "model_id": "model-a", "final_result": '{"x": 1}'},
    )["pass"] is True


# ── static_json determinism ──────────────────────────────────────────────

def test_static_json_numeric_tolerance():
    from modus.evaluation.scorers.static_json import evaluate_static_json

    # 1 vs 1.0 and 5% error bandwidth pass; 12% error fails at default 5%.
    assert evaluate_static_json(
        {"expected": {"amount": 1}}, {"final_result": '{"amount": 1.0}', "events": []},
    )["strict"] is True
    assert evaluate_static_json(
        {"expected": {"amount": 100}}, {"final_result": '{"amount": 104}', "events": []},
    )["strict"] is True
    assert evaluate_static_json(
        {"expected": {"amount": 100}}, {"final_result": '{"amount": 112}', "events": []},
    )["strict"] is False


def test_static_json_delta1():
    from modus.evaluation.scorers.static_json import evaluate_static_json

    score = evaluate_static_json(
        {"expected": {"count": 5}, "match": {"delta1": True}},
        {"final_result": '{"count": 6}', "events": []},
    )
    assert score["strict"] is True
    # Without delta1, 5 vs 6 at default 5% tolerance fails.
    assert evaluate_static_json(
        {"expected": {"count": 5}}, {"final_result": '{"count": 6}', "events": []},
    )["strict"] is False


def test_static_json_range_interval():
    from modus.evaluation.scorers.static_json import (
        evaluate_static_json,
        flatten_answer,
    )

    # start_point/end_point collapses to an interval leaf.
    assert flatten_answer({"start_point": 1, "end_point": 10}) == {"range": [1.0, 10.0]}
    # predicted interval inside the expected interval passes.
    score = evaluate_static_json(
        {"expected": {"start_point": 1, "end_point": 10}},
        {"final_result": '{"start_point": 5, "end_point": 6}', "events": []},
    )
    assert score["strict"] is True and score["diffs"][0]["mode"] == "interval"
    # Disjoint intervals fail.
    assert evaluate_static_json(
        {"expected": {"start_point": 1, "end_point": 10}},
        {"final_result": '{"start_point": 50, "end_point": 60}', "events": []},
    )["strict"] is False


def test_static_json_metrics_and_noise_parsing():
    from modus.evaluation.scorers.static_json import (
        extract_answer,
        evaluate_static_json,
        flatten_answer,
    )

    # Fence + Answer-prefix + balanced-bracket noise parsing.
    assert extract_answer("Here: ```json\n{\"a\": 1}\n```") == '{"a": 1}'
    assert extract_answer("Answer: {\"a\": 1} trailing") == '{"a": 1}'
    assert extract_answer("result is {\"a\": {\"b\": [1, 2]}} done") == '{"a": {"b": [1, 2]}}'

    # Precision/recall/F1 with an extra predicted key.
    score = evaluate_static_json(
        {"expected": {"a": 1, "b": 2}},
        {"final_result": '{"a": 1, "c": 3}', "events": []},
    )
    assert score["strict"] is False
    assert score["matched"] == 1
    assert score["precision"] == pytest.approx(0.5)   # 1 of 2 predicted matched
    assert score["recall"] == pytest.approx(0.5)      # 1 of 2 expected matched
    assert score["f1"] == pytest.approx(0.5)
    assert "b" in score["missing_keys"]
    assert "c" in score["extra_keys"]

    # Full strict match has F1 == 1.0.
    full = evaluate_static_json(
        {"expected": {"a": 1, "b": 2}},
        {"final_result": '{"a": 1, "b": 2}', "events": []},
    )
    assert full["strict"] is True and full["f1"] == 1.0

    # No structured answer recovers → not passing, missing all expected keys.
    empty = evaluate_static_json(
        {"expected": {"a": 1}}, {"final_result": "I have no idea", "events": []},
    )
    assert empty["strict"] is False
    assert empty["expected_count"] == 1 and empty["matched"] == 0


def test_static_json_is_deterministic_and_pure():
    from modus.evaluation.scorers.static_json import evaluate_static_json

    scenario = {"expected": {"x": {"y": 10}, "n": [1, 2, 3]}, "match": {"tolerance": 0.1}}
    traj = {"final_result": '{"x": {"y": 10.5}, "n": [1, 2, 3]}', "events": []}
    first = evaluate_static_json(scenario, traj)
    for _ in range(5):
        assert evaluate_static_json(scenario, traj) == first


# ── EvalReport ───────────────────────────────────────────────────────────

def test_evaluate_report_aggregates_pass_fail_and_cost(eval_db):
    from modus.evaluation import EvalReport, Evaluator

    database = eval_db
    session = database.create_session("report")
    ok_run = _admit_run(database, run_id="run-ok", session_id=session["id"])
    bad_run = _admit_run(database, run_id="run-bad", session_id=session["id"])
    _settle_completed(database, run_id=ok_run, session_id=session["id"],
                      budget={"total_tokens": 100, "input_tokens": 40, "output_tokens": 60})
    _settle_completed(database, run_id=bad_run, session_id=session["id"],
                      budget={"total_tokens": 50, "input_tokens": 20, "output_tokens": 30})

    evaluator = Evaluator()
    report = evaluator.evaluate([
        (_scenario(scenario_id="s1", expected={"total": 100}), ok_run),
        (_scenario(scenario_id="s2", expected={"total": 999}), bad_run),
    ])
    wrapped = EvalReport(report)
    assert wrapped.passed is False
    assert wrapped.summary["runs"] == 2
    assert wrapped.summary["passed"] == 1
    assert wrapped.summary["failed"] == 1
    assert wrapped.summary["total_tokens"] == 150
    assert len(wrapped.failed_scenarios) == 1
    assert wrapped.failed_scenarios[0]["scenario_id"] == "s2"
    for scenario in wrapped.scenarios:
        assert set(scenario) >= {"scenario_id", "pass", "tokens", "cost_usd", "latency_ms"}


def test_evaluate_report_percentiles():
    from modus.evaluation.report import build_report

    scores = [
        {"scenario_id": "s", "run_id": "r1", "pass": True, "partial": True,
         "total_tokens": 10, "latency_ms": 100, "cost_usd": 0.01},
        {"scenario_id": "s", "run_id": "r2", "pass": True, "partial": True,
         "total_tokens": 20, "latency_ms": 400, "cost_usd": 0.02},
        {"scenario_id": "s", "run_id": "r3", "pass": False, "partial": False,
         "total_tokens": 30, "latency_ms": 900, "cost_usd": 0.03},
    ]
    report = build_report(scores)
    scenario = report["scenarios"][0]
    assert scenario["latency_ms"]["p50"] == pytest.approx(400.0)
    assert scenario["latency_ms"]["p95"] == pytest.approx(900.0)
    assert report["summary"]["total_tokens"] == 60
    assert report["summary"]["cost_usd"] == pytest.approx(0.06)


# ── CLI evaluate ─────────────────────────────────────────────────────────

def test_cli_evaluate_run_subcommand(eval_db, capsys, monkeypatch):
    """``modus evaluate --run <id>`` scores an already-completed run."""
    from modus.entrypoints import cli

    database = eval_db
    session = database.create_session("cli eval")
    run_id = _admit_run(database, run_id="run-cli", session_id=session["id"],
                        objective="compute total")
    _settle_completed(database, run_id=run_id, session_id=session["id"])

    def _fake_db_startup(db_module):
        # The CLI's _db_startup would try to acquire the writer lease on the
        # real ~/.modus; reuse the test-isolated db module as-is.
        db_module.init_db()

    monkeypatch.setattr(cli, "_db_startup", _fake_db_startup)
    # Point the CLI's data dir at the isolated dir so _repl/db lookups align.
    monkeypatch.setattr(cli, "_data_dir", lambda: database.DB_DIR)

    cli.evaluate_cmd(run=run_id, suite=None, scorer="static_json")
    out = capsys.readouterr().out
    assert f"run {run_id}" in out or run_id in out
    assert "pass" in out.lower() or "strict" in out.lower() or "f1" in out.lower()


def test_cli_evaluate_suite_dir(eval_db, tmp_path, capsys, monkeypatch):
    """``modus evaluate --suite <dir>`` scores a batch of scenario files."""
    from modus.entrypoints import cli

    database = eval_db
    session = database.create_session("cli suite")
    run_id = _admit_run(database, run_id="run-suite", session_id=session["id"])
    _settle_completed(database, run_id=run_id, session_id=session["id"])

    suite_dir = tmp_path / "suite"
    suite_dir.mkdir()
    (suite_dir / "one.json").write_text(json.dumps({
        "scenario_id": "suite-one", "expected": {"total": 100}, "run_id": run_id,
    }), encoding="utf-8")

    monkeypatch.setattr(cli, "_db_startup", lambda db_module: db_module.init_db())
    monkeypatch.setattr(cli, "_data_dir", lambda: database.DB_DIR)

    cli.evaluate_cmd(run=None, suite=str(suite_dir), scorer="static_json")
    out = capsys.readouterr().out
    assert "suite-one" in out
    assert "scenarios" in out.lower() or "passed" in out.lower()
