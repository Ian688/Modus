from modus.desktop.stopping import DecisionAction, ProgressCheckpoint, decide


def checkpoint(**changes):
    data = {
        "completed_required": 1, "total_required": 2,
        "verified_criteria": 1, "total_criteria": 2,
    }
    data.update(changes)
    return ProgressCheckpoint(**data)


def test_hard_budget_always_wins():
    decision = decide(
        [checkpoint(completed_required=2, verified_criteria=2)],
        budget_exhausted="token_limit",
    )
    assert decision.action is DecisionAction.STOP_BUDGET
    assert decision.confidence == 1.0


def test_success_requires_tasks_criteria_and_dependencies_closed():
    successful = checkpoint(
        completed_required=2, verified_criteria=2, unresolved_dependencies=0,
    )
    blocked = checkpoint(
        completed_required=2, verified_criteria=2, unresolved_dependencies=1,
    )
    assert decide([successful]).action is DecisionAction.STOP_SUCCESS
    assert decide([blocked]).action is DecisionAction.CONTINUE


def test_conflict_creates_arbitration_instead_of_endless_worker_continuation():
    decision = decide([checkpoint(conflicting_conclusions=1, new_evidence=4)])
    assert decision.action is DecisionAction.ARBITRATE
    assert "arbitration" in decision.reason


def test_revision_failure_or_context_pressure_redecomposes():
    assert decide([checkpoint(revision_failures=2)]).action is DecisionAction.REDECOMPOSE
    assert decide([checkpoint(context_pressure=0.95)]).action is DecisionAction.REDECOMPOSE


def test_three_low_progress_checkpoints_stop_as_stalled():
    low = checkpoint(new_evidence=0, effective_diff_lines=0, verification_delta=0)
    decision = decide([low, low, low])
    assert decision.action is DecisionAction.STOP_STALLED
    assert decision.metrics["window"] == [0.0, 0.0, 0.0]


def test_progress_signal_keeps_work_running_and_is_serializable():
    decision = decide([checkpoint(new_evidence=6, verification_delta=0.2)])
    assert decision.action is DecisionAction.CONTINUE
    assert decision.to_wire()["metrics"]["marginal_progress"] > 0


def test_semantic_collapse_over_trailing_window_converges():
    first = checkpoint(semantic_overlap=0.5)
    second = checkpoint(semantic_overlap=0.95)
    third = checkpoint(semantic_overlap=0.95)
    decision = decide([first, second, third])
    assert decision.action is DecisionAction.STOP_CONVERGED
    assert decision.metrics["overlap_window"] == [0.95, 0.95]


def test_single_high_overlap_is_not_yet_collapse():
    decision = decide([checkpoint(semantic_overlap=0.95)])
    assert decision.action is DecisionAction.CONTINUE


def test_sprt_accepts_convergence_on_stable_high_scores():
    # total_criteria=0 forces the legacy score-delta fallback path.
    rounds = [checkpoint(mean_score=8.0, score_delta=d, semantic_overlap=0.5, total_criteria=0)
              for d in (0.0, 0.1, -0.1, 0.0)]
    decision = decide(rounds)
    assert decision.action is DecisionAction.STOP_CONVERGED
    assert decision.metrics["sprt_llr"] <= decision.metrics["sprt_bound"]


def test_sprt_does_not_converge_when_scores_stay_low():
    rounds = [checkpoint(mean_score=3.0, score_delta=d, semantic_overlap=0.5, total_criteria=0)
              for d in (0.0, 0.0, 0.0, 0.0)]
    decision = decide(rounds)
    assert decision.action is not DecisionAction.STOP_CONVERGED


def test_sprt_needs_minimum_sample_size():
    decision = decide([checkpoint(mean_score=8.0, score_delta=0.0, total_criteria=0)] * 2)
    assert decision.action is DecisionAction.CONTINUE


def test_sprt_accepts_convergence_when_met_criteria_stop_changing():
    # Four rounds where the satisfied-criteria count stabilizes high (4/5 met).
    rounds = [
        checkpoint(verified_criteria=4, total_criteria=5, verification_delta=0.8),
        checkpoint(verified_criteria=4, total_criteria=5, verification_delta=0.8),
        checkpoint(verified_criteria=4, total_criteria=5, verification_delta=0.8),
        checkpoint(verified_criteria=4, total_criteria=5, verification_delta=0.8),
    ]
    decision = decide(rounds)
    assert decision.action is DecisionAction.STOP_CONVERGED
    assert decision.metrics["criteria_ratio"] >= 0.8


def test_sprt_does_not_converge_when_met_criteria_ratio_is_low():
    # Stable but low satisfaction is substantive disagreement, not convergence.
    rounds = [
        checkpoint(verified_criteria=1, total_criteria=3, verification_delta=0.33),
        checkpoint(verified_criteria=1, total_criteria=3, verification_delta=0.33),
        checkpoint(verified_criteria=1, total_criteria=3, verification_delta=0.33),
        checkpoint(verified_criteria=1, total_criteria=3, verification_delta=0.33),
    ]
    decision = decide(rounds)
    assert decision.action is not DecisionAction.STOP_CONVERGED


def test_sprt_continues_while_met_criteria_rise():
    rounds = [
        checkpoint(verified_criteria=2, total_criteria=5, verification_delta=0.4),
        checkpoint(verified_criteria=3, total_criteria=5, verification_delta=0.6),
        checkpoint(verified_criteria=4, total_criteria=5, verification_delta=0.8),
    ]
    decision = decide(rounds)
    assert decision.action is DecisionAction.CONTINUE
