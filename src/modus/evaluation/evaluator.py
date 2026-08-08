"""Evaluator: join a scenario against an already-run trajectory and score it.

The pipeline is:
    trajectory (db run_events or persisted ~/.modus/trajectories/*.json)
    → Evaluator.join(scenario, trajectory) → deterministic scorer → score dict

Re-scoring never re-runs the agent.  ``bind_evaluation`` injects
``run_id``/``scenario_id`` through contextvars so a scorer can attribute its
result without the caller threading identifiers through every call.  Scorers
live in a registry keyed by name; the default ``static_json`` is a pure
deterministic JSON/structured-output comparator, and the optional ``llm_judge``
carries a self-review guard (a judge that shares the trajectory's model is
refused, because judging your own work with the same model is not evidence).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextvars import ContextVar, Token
from typing import Any

from modus.evaluation.scorers.static_json import evaluate_static_json

logger = logging.getLogger(__name__)


class EvaluationError(RuntimeError):
    """Raised for an invalid scenario, trajectory, or scorer request."""


# ── contextvar injection (run_id / scenario_id) ──────────────────────────

_active_run_id: ContextVar[str | None] = ContextVar(
    "modus_eval_run_id", default=None,
)
_active_scenario_id: ContextVar[str | None] = ContextVar(
    "modus_eval_scenario_id", default=None,
)


def active_run_id() -> str | None:
    """Return the run_id bound to the current evaluation context, if any."""
    return _active_run_id.get()


def active_scenario_id() -> str | None:
    """Return the scenario_id bound to the current evaluation context, if any."""
    return _active_scenario_id.get()


def bind_evaluation(*, run_id: str | None = None,
                    scenario_id: str | None = None) -> tuple[Token[str | None], Token[str | None]]:
    """Inject run_id / scenario_id into the current evaluation context.

    Returns the pair of tokens so the caller can ``reset_evaluation`` afterwards
    (both tokens, in the same order) — mirroring ``bind_run_budget`` in the
    runtime.  Nesting is safe: resetting a token restores the prior value.
    """
    run_token = _active_run_id.set(run_id) if run_id is not None else _noop_token()
    scenario_token = _active_scenario_id.set(scenario_id) if scenario_id is not None else _noop_token()
    return run_token, scenario_token


def reset_evaluation(run_token: Token[str | None], scenario_token: Token[str | None]) -> None:
    """Restore the prior context values captured by ``bind_evaluation``."""
    _active_run_id.reset(run_token)
    _active_scenario_id.reset(scenario_token)


def _noop_token() -> Token[str | None]:
    return _active_run_id.set(_active_run_id.get())


# ── scorer registry ──────────────────────────────────────────────────────

# Registered scorers: name -> (fn, description).  ``static_json`` is the
# first-version deterministic scorer.  ``llm_judge`` is deliberately *not* a
# working scorer here: an LLM judge would violate the deterministic-pure-scorer
# boundary (blueprint principle 4).  The name is reserved so ``register_scorer``
# can install a real judge behind the evaluator's self-review guard — a judge
# that shares the trajectory's model is refused, because judging your own work
# with the same model is not evidence.
_SCORER_REGISTRY: dict[str, tuple[Callable[..., dict[str, Any]], str]] = {
    "static_json": (evaluate_static_json, "Deterministic structured-output comparator (no LLM)."),
}


def _self_review_guard(scenario: dict[str, Any], trajectory: dict[str, Any]) -> None:
    """Refuse a judge that shares the trajectory's model (self-review guard).

    A judge scorer is identified by the scenario carrying ``judge_model`` (the
    model asked to judge).  If that model equals the trajectory's ``model_id``,
    the judge would be evaluating its own output — raise EvaluationError instead
    of silently accepting non-evidence.  Scenarios without a ``judge_model`` (all
    deterministic scorers) skip the check entirely.
    """
    judge_model = str(scenario.get("judge_model") or "").strip()
    if not judge_model:
        return
    traj_model = str(trajectory.get("model_id") or "").strip()
    if traj_model and judge_model == traj_model:
        raise EvaluationError(
            f"self-review guard: judge model {judge_model!r} is the trajectory's "
            "own model; judging your own work with the same model is not evidence"
        )


def register_scorer(name: str, fn: Callable[..., dict[str, Any]], *, description: str = "") -> None:
    """Register (or replace) a scorer under a stable name.

    A scorer is a pure function ``fn(scenario, trajectory, **opts) -> score``
    where score is a dict with at least ``pass``/``partial``/``reason``.  The
    blueprint principle-4 rule applies: scorers must be deterministic; the
    ``llm_judge`` exception carries its own self-review guard.
    """
    if not isinstance(name, str) or not name.strip():
        raise EvaluationError("scorer name must be a non-empty string")
    if not callable(fn):
        raise EvaluationError("scorer must be callable")
    _SCORER_REGISTRY[name.strip()] = (fn, str(description or ""))


def get_scorer(name: str) -> Callable[..., dict[str, Any]]:
    """Return the scorer callable registered under ``name``."""
    entry = _SCORER_REGISTRY.get(str(name or ""))
    if entry is None:
        raise EvaluationError(
            f"unknown scorer {name!r}; known: {', '.join(sorted(scorer_names())) or '(none)'}"
        )
    return entry[0]


def scorer_names() -> list[str]:
    """Return the sorted names of all registered scorers."""
    return sorted(_SCORER_REGISTRY)


# ── trajectory loading ───────────────────────────────────────────────────

def _load_trajectory(trajectory: dict[str, Any] | str) -> dict[str, Any]:
    """Normalize a trajectory to a plain dict.

    Accepts a trajectory dict (as produced by ``persist_trajectory`` or a
    scenario-run loader) or a run_id string, which is resolved from the
    persisted trajectory store first and the SQLite ledger second.  A string
    that resolves nowhere raises EvaluationError so a typo'd run_id fails
    loudly instead of scoring an empty trajectory.
    """
    if isinstance(trajectory, dict):
        return trajectory
    if not isinstance(trajectory, str) or not trajectory.strip():
        raise EvaluationError("trajectory must be a dict or a run_id string")
    run_id = trajectory.strip()
    from modus.desktop import db

    stored = db.load_trajectory(run_id)
    if stored is not None:
        return stored
    run = db.get_run(run_id)
    if run is None:
        raise EvaluationError(f"no trajectory or run found for run_id {run_id!r}")
    events = db.get_run_events(run_id)
    return {
        "run_id": run_id,
        "state": str(run.get("state") or ""),
        "stop_reason": str(run.get("stop_reason") or "") or None,
        "mode": str(run.get("mode") or ""),
        "objective": str(run.get("objective") or ""),
        "final_result": str(run.get("final_result") or ""),
        "budget": run.get("budget") or {},
        "events": events,
    }


def _scenario_requires_trajectory(scenario: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract the run_id/scenario_id a scenario references, if any."""
    return (
        str(scenario.get("run_id") or "") or None,
        str(scenario.get("scenario_id") or "") or None,
    )


def _budget_int(trajectory: dict[str, Any], key: str) -> int:
    budget = trajectory.get("budget")
    if isinstance(budget, dict):
        return max(0, int(budget.get(key) or 0))
    return 0


def _estimate_cost(trajectory: dict[str, Any]) -> float:
    """Estimate USD cost from the trajectory's budget via the local price table.

    Best-effort and pure: a missing price table or unknown model falls back to
    the billing module's defaults, and a failure returns 0.0 rather than
    aborting an evaluation.
    """
    budget = trajectory.get("budget")
    if not isinstance(budget, dict):
        return 0.0
    input_tokens = int(budget.get("input_tokens") or 0)
    output_tokens = int(budget.get("output_tokens") or 0)
    if input_tokens <= 0 and output_tokens <= 0:
        return 0.0
    try:
        from modus.desktop.billing import compute_charge_cents, load_pricing, price_for_model

        model_id = str(trajectory.get("model_id") or "")
        price = price_for_model(load_pricing(), model_id)
        cents = compute_charge_cents(
            input_tokens=input_tokens, output_tokens=output_tokens, price=price,
        )
        return round(cents / 100.0, 6)
    except Exception:
        return 0.0


def _trajectory_latency_ms(trajectory: dict[str, Any]) -> float:
    """Wall-clock latency from the first to the last event timestamp (ms).

    Uses the run's own event timestamps (ISO strings) rather than wall time so
    a replay scores the same way every run; 0.0 when no two timestamps parse.
    """
    events = trajectory.get("events")
    if not isinstance(events, list) or not events:
        return 0.0
    from datetime import datetime

    stamps: list[float] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        value = str(event.get("timestamp") or "")
        if not value:
            continue
        try:
            stamps.append(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError):
            continue
    if len(stamps) < 2:
        return 0.0
    return round(max(0.0, (max(stamps) - min(stamps)) * 1000.0), 2)


# ── Evaluator ────────────────────────────────────────────────────────────


class Evaluator:
    """Offline, read-only scorer that joins scenarios against trajectories.

    Usage::

        evaluator = Evaluator()
        score = evaluator.score(scenario, trajectory, scorer="static_json")
        report = evaluator.evaluate([(scenario, trajectory_or_run_id), ...])

    ``evaluate`` binds run_id/scenario_id into contextvars per item so a scorer
    can read them via ``active_run_id`` / ``active_scenario_id``, then scores
    each join and aggregates a per-scenario EvalReport.
    """

    def __init__(self, *, default_scorer: str = "static_json") -> None:
        if default_scorer not in _SCORER_REGISTRY:
            raise EvaluationError(f"unknown default scorer {default_scorer!r}")
        self.default_scorer = default_scorer

    def score(self, scenario: dict[str, Any], trajectory: dict[str, Any] | str,
              *, scorer: str | None = None, **opts: Any) -> dict[str, Any]:
        """Score one scenario × trajectory join with a registered scorer.

        ``scenario`` is a plain dict carrying at least ``expected`` (the
        reference answer) and optionally ``scenario_id``/``run_id``/``match``
        options.  ``trajectory`` is either a trajectory dict or a run_id string.
        The result dict is the scorer output merged with the join identity.
        """
        if not isinstance(scenario, dict) or "expected" not in scenario:
            raise EvaluationError("scenario must be a dict with an 'expected' key")
        trajectory_doc = _load_trajectory(trajectory)
        scorer_name = scorer or self.default_scorer
        fn = get_scorer(scorer_name)
        _self_review_guard(scenario, trajectory_doc)
        score = fn(scenario, trajectory_doc, **opts)
        if not isinstance(score, dict):
            raise EvaluationError(
                f"scorer {scorer_name!r} returned a non-dict score: {type(score).__name__}"
            )
        run_id, scenario_id = _scenario_requires_trajectory(scenario)
        result = dict(score)
        result.setdefault("scorer", scorer_name)
        result.setdefault("run_id", run_id or str(trajectory_doc.get("run_id") or ""))
        result.setdefault("scenario_id", scenario_id or str(scenario.get("scenario_id") or ""))
        # Enrich with the trajectory's own budget/cost/latency so the report can
        # aggregate per scenario without the scorer knowing the run's ledger.
        result.setdefault("total_tokens", _budget_int(trajectory_doc, "total_tokens"))
        result.setdefault("input_tokens", _budget_int(trajectory_doc, "input_tokens"))
        result.setdefault("output_tokens", _budget_int(trajectory_doc, "output_tokens"))
        if result.get("cost_usd") in (None, 0):
            result["cost_usd"] = _estimate_cost(trajectory_doc)
        result.setdefault("latency_ms", _trajectory_latency_ms(trajectory_doc))
        return result

    def evaluate(self, joins: list[tuple[dict[str, Any], dict[str, Any] | str]],
                 *, scorer: str | None = None) -> dict[str, Any]:
        """Score a batch of (scenario, trajectory) joins with context binding.

        Each join is scored with ``run_id``/``scenario_id`` bound into the
        evaluation context so scorers can attribute failures without extra
        parameters.  A single bad join raises EvaluationError (fail loudly); a
        suite runner may instead catch per item to keep going.
        """
        from modus.evaluation.report import build_report

        scores: list[dict[str, Any]] = []
        for scenario, trajectory in joins:
            run_id, scenario_id = _scenario_requires_trajectory(scenario)
            run_token, scenario_token = bind_evaluation(
                run_id=run_id, scenario_id=scenario_id,
            )
            try:
                scores.append(self.score(scenario, trajectory, scorer=scorer))
            finally:
                reset_evaluation(run_token, scenario_token)
        return build_report(scores)
