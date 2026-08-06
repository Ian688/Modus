"""Deterministic, auditable stopping and re-decomposition policy.

This module deliberately contains no LLM calls. It transforms measurable task
checkpoints into a recommendation; RunBudget/RunController remain the only
owners of terminal state transitions.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class DecisionAction(StrEnum):
    CONTINUE = "continue"
    STOP_SUCCESS = "stop_success"
    STOP_BUDGET = "stop_budget"
    STOP_STALLED = "stop_stalled"
    STOP_CONVERGED = "stop_converged"
    REDECOMPOSE = "redecompose"
    ARBITRATE = "arbitrate"


@dataclass(frozen=True, slots=True)
class ProgressCheckpoint:
    completed_required: int
    total_required: int
    verified_criteria: int
    total_criteria: int
    new_evidence: int = 0
    effective_diff_lines: int = 0
    verification_delta: float = 0.0
    unresolved_dependencies: int = 0
    revision_failures: int = 0
    conflicting_conclusions: int = 0
    context_pressure: float = 0.0
    mean_score: float = 0.0
    score_delta: float = 0.0
    semantic_overlap: float = 0.0

    @property
    def marginal_progress(self) -> float:
        """Bounded progress signal based only on observable changes."""
        raw = (
            min(self.new_evidence, 10) / 10 * 0.35
            + min(self.effective_diff_lines, 200) / 200 * 0.25
            + max(0.0, min(self.verification_delta, 1.0)) * 0.40
        )
        return round(max(0.0, min(raw, 1.0)), 4)

    def to_wire(self) -> dict[str, Any]:
        return {
            "completed_required": self.completed_required,
            "total_required": self.total_required,
            "verified_criteria": self.verified_criteria,
            "total_criteria": self.total_criteria,
            "new_evidence": self.new_evidence,
            "effective_diff_lines": self.effective_diff_lines,
            "verification_delta": self.verification_delta,
            "unresolved_dependencies": self.unresolved_dependencies,
            "revision_failures": self.revision_failures,
            "conflicting_conclusions": self.conflicting_conclusions,
            "context_pressure": self.context_pressure,
            "mean_score": self.mean_score,
            "score_delta": self.score_delta,
            "semantic_overlap": self.semantic_overlap,
            "marginal_progress": self.marginal_progress,
        }


@dataclass(frozen=True, slots=True)
class StopDecision:
    action: DecisionAction
    reason: str
    confidence: float
    metrics: dict[str, Any]

    def to_wire(self) -> dict[str, Any]:
        return {
            "action": self.action.value, "reason": self.reason,
            "confidence": self.confidence, "metrics": self.metrics,
        }


def sprt_log_likelihood(
    deltas: list[float], *, mu: float = 0.5, sigma: float = 0.3,
) -> float:
    """Cumulative log-likelihood ratio: H0 converged (delta≈0) vs H1 improving.

    Each delta is assumed to be drawn from N(0, σ²) under H0 and N(μ, σ²) under
    H1.  A negative cumulative LLR accumulates evidence for H0 (converged); a
    crossing of ``ln(β/(1-α))`` accepts H0 with error bounds α/β.
    """
    if not deltas:
        return 0.0
    return sum((float(d) - mu / 2) * mu / sigma**2 for d in deltas)


def semantic_collapse(
    overlaps: list[float], *, threshold: float = 0.90, window: int = 2,
) -> bool:
    """True when the trailing window of successive-output overlap is all high."""
    if len(overlaps) < window:
        return False
    return all(overlap >= threshold for overlap in overlaps[-window:])


def decide(
    checkpoints: list[ProgressCheckpoint], *, budget_exhausted: str | None = None,
    stall_window: int = 3, stall_threshold: float = 0.05,
    semantic_threshold: float = 0.90, semantic_window: int = 2,
    sprt_alpha: float = 0.10, sprt_beta: float = 0.10, min_sprt_samples: int = 4,
    sprt_min_ratio: float = 0.8,
) -> StopDecision:
    """Return one explainable recommendation from current checkpoints."""
    if budget_exhausted:
        return StopDecision(
            DecisionAction.STOP_BUDGET, f"hard budget reached: {budget_exhausted}", 1.0,
            {"budget_reason": budget_exhausted},
        )
    if not checkpoints:
        return StopDecision(DecisionAction.CONTINUE, "no checkpoint yet", 1.0, {})

    current = checkpoints[-1]
    metrics = current.to_wire()
    tasks_complete = current.total_required > 0 and current.completed_required >= current.total_required
    criteria_verified = current.total_criteria > 0 and current.verified_criteria >= current.total_criteria
    if tasks_complete and criteria_verified and current.unresolved_dependencies == 0:
        return StopDecision(
            DecisionAction.STOP_SUCCESS,
            "all required tasks and success criteria have verified evidence", 1.0, metrics,
        )
    if current.conflicting_conclusions > 0:
        return StopDecision(
            DecisionAction.ARBITRATE,
            "worker conclusions conflict; create a focused arbitration task", 0.95, metrics,
        )
    if current.revision_failures >= 2 or current.context_pressure >= 0.9:
        reason = (
            "task failed revision twice" if current.revision_failures >= 2
            else "task context exceeds 90% of its role budget"
        )
        return StopDecision(
            DecisionAction.REDECOMPOSE,
            reason + "; split into smaller dependency-aware tasks", 0.9, metrics,
        )

    overlaps = [item.semantic_overlap for item in checkpoints]
    if semantic_collapse(overlaps, threshold=semantic_threshold, window=semantic_window):
        return StopDecision(
            DecisionAction.STOP_CONVERGED,
            f"semantic overlap stayed above {semantic_threshold:.2f} for {semantic_window} rounds; "
            "further revision only changes expression",
            0.9,
            {**metrics, "overlap_window": overlaps[-semantic_window:]},
        )
    # SPRT accepts convergence when the count of satisfied criteria stops
    # changing while most criteria are already met.  A low satisfaction ratio
    # with stable counts is substantive disagreement, not convergence, so it
    # must not be accepted here.
    has_criteria = any(item.total_criteria > 0 for item in checkpoints)
    if has_criteria:
        deltas: list[float] = []
        previous: int | None = None
        for item in checkpoints:
            if item.total_criteria > 0:
                current = item.verified_criteria
                if previous is not None:
                    deltas.append(float(current - previous))
                previous = current
        ratios = [
            item.verified_criteria / item.total_criteria
            for item in checkpoints
            if item.total_criteria > 0
        ]
        if (
            len(deltas) >= max(1, min_sprt_samples - 1)
            and ratios and min(ratios) >= sprt_min_ratio
        ):
            llr = sprt_log_likelihood(deltas)
            bound = math.log(max(1e-9, sprt_beta) / max(1e-9, 1 - sprt_alpha))
            if llr <= bound:
                return StopDecision(
                    DecisionAction.STOP_CONVERGED,
                    f"SPRT accepted convergence on criteria: cumulative LLR {llr:.3f} <= {bound:.3f}",
                    0.9,
                    {**metrics, "sprt_llr": llr, "sprt_bound": bound, "criteria_ratio": min(ratios)},
                )
    else:
        # Legacy callers that never feed verified criteria fall back to the
        # subjective score-delta signal so existing decision tables keep working.
        score_deltas = [item.score_delta for item in checkpoints]
        scores = [item.mean_score for item in checkpoints]
        if (
            len(score_deltas) >= max(1, min_sprt_samples)
            and scores and min(scores) >= 6.0
        ):
            llr = sprt_log_likelihood(score_deltas)
            bound = math.log(max(1e-9, sprt_beta) / max(1e-9, 1 - sprt_alpha))
            if llr <= bound:
                return StopDecision(
                    DecisionAction.STOP_CONVERGED,
                    f"SPRT accepted convergence: cumulative LLR {llr:.3f} <= {bound:.3f}",
                    0.9,
                    {**metrics, "sprt_llr": llr, "sprt_bound": bound},
                )

    window = checkpoints[-max(1, stall_window):]
    if len(window) >= max(1, stall_window) and all(item.marginal_progress < stall_threshold for item in window):
        return StopDecision(
            DecisionAction.STOP_STALLED,
            f"marginal progress stayed below {stall_threshold:.3f} for {len(window)} checkpoints",
            0.85,
            {**metrics, "window": [item.marginal_progress for item in window]},
        )
    return StopDecision(
        DecisionAction.CONTINUE, "more required work or verification remains", 0.8, metrics,
    )
