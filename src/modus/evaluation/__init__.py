"""Offline trajectory re-scoring: objective runtime-quality signals (Wave5 E1).

An ``Evaluator`` joins a scenario against an already-run agent trajectory (from
``modus.desktop.db`` run_events or a persisted ``~/.modus/trajectories/*.json``
file), runs a deterministic scorer from the registry, and produces an
``EvalReport``.  Re-scoring never re-runs the agent; the trajectory is a replay
artifact.

Safety invariants (design doc): evaluation is a read-only, offline pass over
Modus's own data plane — it never touches approvals, capability gates, or the
writer lease, and every scorer is a pure deterministic function (blueprint
principle 4), so a scoring boundary never depends on an LLM classifier.
"""

from modus.evaluation.evaluator import (
    Evaluator,
    EvaluationError,
    active_run_id,
    active_scenario_id,
    bind_evaluation,
    get_scorer,
    register_scorer,
    scorer_names,
    reset_evaluation,
)
from modus.evaluation.report import EvalReport, aggregate_reports, build_report

__all__ = [
    "Evaluator",
    "EvaluationError",
    "EvalReport",
    "active_run_id",
    "active_scenario_id",
    "aggregate_reports",
    "bind_evaluation",
    "build_report",
    "get_scorer",
    "register_scorer",
    "reset_evaluation",
    "scorer_names",
]
