"""EvalReport: per-scenario aggregation of scores, tokens, cost and latency.

The report is a plain dict (no I/O) that aggregates a batch of scorer results
per scenario and across the suite: pass/fail counts, strict/partial status,
token usage from the run budget, estimated cost (via the local billing price
table), and latency percentiles (p50/p95) from event timestamps or provided
``latency_ms`` values.
"""

from __future__ import annotations

import math
from typing import Any


class EvalReport:
    """Typed wrapper over the aggregated report dict (immutable-ish accessor).

    ``EvalReport(report)`` exposes ``report`` (the plain dict from
    ``build_report``) plus read-only conveniences: ``pass`` (overall suite
    result), ``summary``, ``scenarios`` and ``failed_scenarios``.  The
    underlying value stays JSON-serializable so a CLI can print it directly.
    """

    __slots__ = ("report",)

    def __init__(self, report: dict[str, Any]) -> None:
        if not isinstance(report, dict):
            raise TypeError("EvalReport requires a report dict")
        self.report = report

    @property
    def summary(self) -> dict[str, Any]:
        return self.report.get("summary") or {}

    @property
    def scenarios(self) -> list[dict[str, Any]]:
        return self.report.get("scenarios") or []

    @property
    def passed(self) -> bool:
        return bool(self.summary.get("pass"))

    @property
    def failed_scenarios(self) -> list[dict[str, Any]]:
        return [scenario for scenario in self.scenarios if not scenario.get("pass")]

    def to_dict(self) -> dict[str, Any]:
        return self.report

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        summary = self.summary
        return (
            f"EvalReport(pass={summary.get('pass')}, "
            f"runs={summary.get('runs')}, passed={summary.get('passed')}, "
            f"failed={summary.get('failed')})"
        )


def build_report(scores: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate scorer outputs into an EvalReport dict.

    ``scores`` is the list returned by ``Evaluator.score`` (each a dict with at
    least ``pass``/``partial``/``run_id``/``scenario_id``).  Latency is taken
    from each score's ``latency_ms`` field when present; otherwise derived from
    a ``started_at``/``ended_at`` pair in the score or left 0.
    """
    items = [dict(score) for score in scores]
    by_scenario: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        scenario_id = str(item.get("scenario_id") or item.get("run_id") or "")
        by_scenario.setdefault(scenario_id, []).append(item)

    scenarios: list[dict[str, Any]] = []
    for scenario_id, group in sorted(by_scenario.items()):
        first = group[0]
        passed = sum(1 for item in group if item.get("pass"))
        partial = sum(1 for item in group if item.get("partial") and not item.get("pass"))
        token_usage = {
            "total_tokens": sum(_int(item, "total_tokens") for item in group),
            "input_tokens": sum(_int(item, "input_tokens") for item in group),
            "output_tokens": sum(_int(item, "output_tokens") for item in group),
        }
        latencies = sorted(
            float(item.get("latency_ms") or 0) for item in group if (item.get("latency_ms") or 0) > 0
        )
        scenarios.append({
            "scenario_id": scenario_id,
            "runs": len(group),
            "passed": passed,
            "failed": len(group) - passed,
            "partial": partial,
            "pass": passed == len(group) and len(group) > 0,
            "score": _scenario_score(group),
            "tokens": token_usage,
            "cost_usd": round(
                sum(float(item.get("cost_usd") or 0) for item in group), 6,
            ),
            "latency_ms": {
                "p50": _percentile(latencies, 0.50),
                "p95": _percentile(latencies, 0.95),
            },
            "reasons": sorted({str(item.get("reason") or "") for item in group if item.get("reason")}),
            "run_ids": sorted({str(item.get("run_id") or "") for item in group if item.get("run_id")}),
        })

    all_passed = sum(1 for item in items if item.get("pass"))
    total = len(items)
    return {
        "schema": "modus.eval-report.v1",
        "scenarios": scenarios,
        "summary": {
            "scenarios": len(by_scenario),
            "runs": total,
            "passed": all_passed,
            "failed": total - all_passed,
            "pass": all_passed == total and total > 0,
            "precision": _avg(item.get("precision"), items),
            "recall": _avg(item.get("recall"), items),
            "f1": _avg(item.get("f1"), items),
            "total_tokens": sum(_int(item, "total_tokens") for item in items),
            "cost_usd": round(sum(float(item.get("cost_usd") or 0) for item in items), 6),
        },
    }


def _scenario_score(group: list[dict[str, Any]]) -> float:
    if not group:
        return 0.0
    return round(sum(float(item.get("pass") and 1 or 0) for item in group) / len(group), 4)


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    index = max(0, min(len(sorted_values) - 1, int(math.ceil(percentile * len(sorted_values)) - 1)))
    return round(sorted_values[index], 2)


def _avg(value: Any, items: list[dict[str, Any]]) -> float:
    numeric = [float(item.get(value) or 0) for item in items]
    if not numeric:
        return 0.0
    return round(sum(numeric) / len(numeric), 4)


def _int(value: dict[str, Any], key: str) -> int:
    return max(0, int(value.get(key) or 0))


def aggregate_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine several EvalReports (e.g. one per scenario file) into one.

    Each report's per-scenario aggregates are re-flattened into per-scenario
    pseudo-scores and re-aggregated by ``build_report`` so the combined summary
    (runs/passed/failed/tokens/cost) is consistent across files.
    """
    scores: list[dict[str, Any]] = []
    for report in reports:
        for scenario in report.get("scenarios") or []:
            run_ids = scenario.get("run_ids") or []
            if run_ids:
                runs = run_ids
            else:
                runs = [scenario.get("scenario_id") or "?"]
            for run_id in runs:
                scores.append({
                    "scenario_id": scenario.get("scenario_id"),
                    "run_id": run_id,
                    "pass": bool(scenario.get("pass")),
                    "partial": bool(scenario.get("partial")),
                    "precision": scenario.get("precision") or scenario.get("score") or 0.0,
                    "recall": scenario.get("recall") or 0.0,
                    "f1": scenario.get("f1") or scenario.get("score") or 0.0,
                    "total_tokens": (scenario.get("tokens") or {}).get("total_tokens", 0),
                    "input_tokens": (scenario.get("tokens") or {}).get("input_tokens", 0),
                    "output_tokens": (scenario.get("tokens") or {}).get("output_tokens", 0),
                    "cost_usd": scenario.get("cost_usd", 0.0),
                    "latency_ms": (scenario.get("latency_ms") or {}).get("p50", 0.0),
                })
    return build_report(scores)
