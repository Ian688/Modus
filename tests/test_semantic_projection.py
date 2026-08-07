from __future__ import annotations

import json

from modus.desktop.semantic_projection import (
    SEMANTIC_RUN_SCHEMA,
    project_semantic_run,
)


def event(
    sequence: int,
    event_type: str,
    *,
    payload: dict | None = None,
    event_id: str | None = None,
    status: str = "completed",
    task_id: str = "task-root",
    revision: int = 0,
    actor: dict | None = None,
) -> dict:
    result = {
        "event_id": event_id or f"evt-{sequence}",
        "run_id": "run-1",
        "workspace_id": "ws-1",
        "task_id": task_id,
        "sequence": sequence,
        "revision": revision,
        "type": event_type,
        "status": status,
        "payload": payload or {},
    }
    if actor is not None:
        result["actor"] = actor
    return result


def verification(
    sequence: int,
    status: str,
    *,
    event_id: str | None = None,
    command: str = "pytest tests/test_smoke.py",
    passed: int = 0,
    failed: int = 0,
) -> dict:
    evidence = {
        "schema": "modus.verification.v1",
        "kind": "tests",
        "status": status,
        "command": command,
        "path": ".",
        "exit_code": 0 if status == "passed" else 1,
        "duration_seconds": 1.25,
        "counts": {"passed": passed, "failed": failed},
        "output": "raw output must not enter the semantic evidence projection",
    }
    return event(
        sequence,
        "tool_result",
        event_id=event_id,
        status="completed" if status == "passed" else "failed",
        payload={
            "tool_call_id": f"call-{sequence}",
            "name": "run_tests",
            "result": json.dumps(evidence),
            "is_error": status != "passed",
            "metadata": {
                "operation": "verification",
                "changed": False,
                "verification": {
                    "schema": "modus.verification.v1",
                    "status": status,
                    "exit_code": evidence["exit_code"],
                    "duration_seconds": 1.25,
                    "counts": evidence["counts"],
                },
            },
        },
    )


def completed(sequence: int = 10) -> dict:
    return event(
        sequence,
        "run_completed",
        payload={
            "stop_reason": "completed",
            "total_turns": 4,
            "total_tokens": 32000,
            "budget": {"elapsed_seconds": 18},
        },
    )


def project(
    events: list[dict],
    *,
    state: str = "completed",
    stop_reason: str = "completed",
) -> dict:
    return project_semantic_run(
        run={
            "run_id": "run-1",
            "workspace_id": "ws-1",
            "state": state,
            "stop_reason": stop_reason,
        },
        events=events,
        tasks=[
            {
                "task_id": "task-root",
                "task_kind": "root",
                "title": "运行 smoke test",
            }
        ],
        artifacts=[{"artifact_id": "artifact-1"}],
    )


def test_failed_verification_followed_by_pass_is_success_with_recovery() -> None:
    semantic = project(
        [
            event(1, "user_message", payload={"markdown": "smoke"}),
            verification(5, "failed", failed=1),
            verification(8, "passed", passed=4),
            completed(),
        ]
    )

    assert semantic["schema"] == SEMANTIC_RUN_SCHEMA
    assert semantic["outcome"] == {
        "status": "succeeded",
        "summary": "任务已完成并通过验证",
        "confidence": "verified",
        "attention": "caution",
        "stop_reason": "completed",
        "verified": True,
        "recovery_count": 1,
        "requires_user_action": False,
        "source_event_id": "evt-10",
    }
    assert [item["status"] for item in semantic["evidence"]] == [
        "failed",
        "passed",
    ]
    assert semantic["evidence"][-1]["current"] is True
    assert semantic["recoveries"][0]["scope"] == "verification"
    assert semantic["recoveries"][0]["summary"] == "验证失败后重试通过"
    assert "output" not in semantic["evidence"][0]
    assert semantic["metrics"] == {
        "duration_seconds": 18.0,
        "turns": 4,
        "tokens": 32000,
    }


def test_mutation_after_passing_verification_makes_evidence_stale() -> None:
    semantic = project(
        [
            verification(3, "passed", passed=4),
            event(
                5,
                "tool_result",
                payload={
                    "name": "edit_file",
                    "is_error": False,
                    "result": "ok",
                    "metadata": {
                        "changed": True,
                        "operation": "edit",
                        "path": "app.py",
                    },
                },
            ),
            completed(7),
        ]
    )

    assert semantic["evidence"][-1]["current"] is False
    assert semantic["outcome"]["status"] == "succeeded"
    assert semantic["outcome"]["verified"] is False
    assert semantic["outcome"]["confidence"] == "unverified"
    assert semantic["outcome"]["attention"] == "caution"


def test_retried_non_verification_tool_is_recovery_not_run_failure() -> None:
    semantic = project(
        [
            event(
                2,
                "tool_result",
                payload={
                    "name": "bash",
                    "is_error": True,
                    "result": "bad interpreter",
                },
            ),
            event(
                4,
                "tool_result",
                payload={
                    "name": "bash",
                    "is_error": False,
                    "result": "Python 3.12",
                },
            ),
            completed(6),
        ]
    )

    assert semantic["outcome"]["status"] == "succeeded"
    assert semantic["outcome"]["attention"] == "caution"
    assert semantic["outcome"]["recovery_count"] == 1
    assert semantic["recoveries"][0]["scope"] == "tool:bash"


def test_budget_terminal_is_incomplete_and_blocked() -> None:
    terminal = event(
        4,
        "run_error",
        status="failed",
        payload={"stop_reason": "token_limit", "message": "limit"},
    )
    semantic = project(
        [terminal], state="failed", stop_reason="token_limit"
    )

    assert semantic["outcome"]["status"] == "incomplete"
    assert semantic["outcome"]["attention"] == "blocked"
    assert semantic["outcome"]["stop_reason"] == "token_limit"


def test_running_pending_approval_requires_user_action() -> None:
    semantic = project_semantic_run(
        run={"run_id": "run-1", "state": "running"},
        events=[
            event(
                2,
                "approval_request",
                payload={"approval_id": "approval-1"},
            )
        ],
    )

    assert semantic["outcome"]["status"] == "running"
    assert semantic["outcome"]["attention"] == "action_required"
    assert semantic["outcome"]["requires_user_action"] is True


def test_projection_is_order_independent_and_keeps_latest_revision() -> None:
    old = event(
        3,
        "tool_result",
        event_id="verification",
        revision=0,
        payload={
            "name": "run_tests",
            "result": "{}",
            "is_error": True,
        },
    )
    latest = verification(
        3, "passed", event_id="verification", passed=2
    )
    latest["revision"] = 2

    semantic = project([completed(5), latest, old])

    assert len(semantic["evidence"]) == 1
    assert semantic["evidence"][0]["status"] == "passed"
    assert semantic["projection_cursor"] == {
        "sequence": 5,
        "event_revision": 0,
    }
    assert semantic["source_event_ids"] == ["verification", "evt-5"]


def test_activities_project_every_actor_with_mode_independent_shape() -> None:
    """default/MOA/Peri all produce the same activity vocabulary."""
    semantic = project(
        [
            event(1, "user_message", payload={"markdown": "整理文稿"}),
            event(
                2,
                "tool_result",
                payload={
                    "name": "read_file",
                    "is_error": False,
                    "result": "1: content",
                    "metadata": {"operation": "read", "path": "docs/a.md"},
                },
            ),
            verification(3, "passed", passed=4),
            completed(),
        ]
    )

    activities = semantic["activities"]
    kinds = [item["kind"] for item in activities]
    assert "message" in kinds
    assert "tool" in kinds
    assert "verify" in kinds
    # Every activity carries the uniform board-consumable fields.
    for item in activities:
        assert item["actor"]
        assert item["phase"]
        assert item["action"]
        assert item["sequence"] >= 1
    # Verification activity folds in its counts.
    verify = next(item for item in activities if item["kind"] == "verify")
    assert verify["detail"] == "4 通过 · 0 失败"
    assert verify["status"] == "ok"
    # The read tool activity carries its path as detail.
    read = next(item for item in activities if item["action"] == "读取文件")
    assert read["detail"] == "docs/a.md"


def test_phases_collapse_consecutive_stages_and_keep_actor_sets() -> None:
    semantic = project(
        [
            event(1, "user_message", payload={"markdown": "并行分析"}),
            event(
                3,
                "subagent_tool_result",
                actor={"kind": "subagent", "id": "worker_1", "label": "Worker 1"},
                payload={"name": "read_file", "is_error": False, "result": "x"},
            ),
            event(
                4,
                "subagent_response",
                actor={"kind": "subagent", "id": "worker_1", "label": "Worker 1"},
                payload={"markdown": "分析完成"},
            ),
            event(
                5,
                "host_review",
                actor={"kind": "host", "id": "primary", "label": "Host"},
                payload={"markdown": "采纳"},
            ),
            completed(),
        ]
    )

    phases = semantic["phases"]
    assert phases, "phases should not be empty"
    assert phases[0]["kind"] == "analyzing"
    # The worker tool call lands in executing; its response and the host review
    # both collapse into the reviewing stage with distinct actors.
    executing = next(p for p in phases if p["kind"] == "executing")
    assert executing["count"] == 1
    assert any("Worker 1" in actor for actor in executing["actors"])
    reviewing = next(p for p in phases if p["kind"] == "reviewing")
    assert reviewing["count"] == 2
    assert any("Host" in actor for actor in reviewing["actors"])


def test_activities_reject_missing_actor_with_host_fallback() -> None:
    """Events without an actor field (embedder/tests) fall back to host."""
    semantic = project([completed()])
    # run_completed has no actor in the test helper; activities stay valid.
    assert all(item["actor"] for item in semantic["activities"])


def test_retrospective_is_derived_and_bounded():
    """Retrospective summarizes the run's ending from existing projections."""
    from modus.desktop.semantic_projection import _retrospective

    retro = _retrospective(
        outcome={"stop_reason": "max_turns", "status": "incomplete", "confidence": "unverified"},
        phases=[{"kind": "executing", "count": 5}, {"kind": "verifying", "count": 1}],
        evidence=[{"status": "failed"}],
        metrics={"duration_seconds": 10.0, "turns": 6, "tokens": 100},
        turn_records=[
            {"turn": i, "text_chars": 0, "tool_calls": 1, "tool_successes": 0, "tool_errors": 1}
            for i in range(60)  # more than 50 -> capped
        ],
    )

    assert retro["stop_reason"] == "max_turns"
    assert retro["verification"] == "unverified"
    assert retro["evidence_attempts"] == 1
    assert retro["evidence_passed"] == 0
    # Turn strip is bounded to the newest 50.
    assert len(retro["turn_strip"]) == 50
    assert retro["turn_strip"][-1]["turn"] == 59
    assert retro["metrics"]["tokens"] == 100
