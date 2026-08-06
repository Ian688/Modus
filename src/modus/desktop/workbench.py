"""Versioned Run/Task/Part/Artifact read model for the Desktop workbench."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from modus.desktop.semantic_projection import project_semantic_run


WORKBENCH_SCHEMA = "modus.workbench.v1"
EVENT_SCHEMA = "modus.agent-event.v2"


@dataclass(frozen=True, slots=True)
class PartIdentity:
    """The stable relationship carried by every rendered event part."""

    part_id: str
    run_id: str
    task_id: str | None = None
    artifact_ids: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {
            "part_id": self.part_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "artifact_ids": list(self.artifact_ids),
        }


def build_workbench_snapshot(session_id: str) -> dict[str, Any]:
    """Project the authoritative ledger into one browser-safe workbench DTO."""
    from modus.desktop.db import (
        get_session,
        get_workspace,
        list_run_artifacts,
        get_run_events,
        list_run_tasks,
        list_runs_for_session,
    )

    session = get_session(session_id)
    if session is None:
        raise ValueError("session not found")
    workspace = get_workspace(str(session.get("workspace_id") or ""))
    runs: list[dict[str, Any]] = []
    for run in list_runs_for_session(session_id):
        run_id = str(run["run_id"])
        events = get_run_events(run_id)
        tasks = [public_task(task) for task in list_run_tasks(run_id)]
        artifacts = [public_artifact_ref(item) for item in list_run_artifacts(run_id)]
        review = build_run_review(events, state=str(run.get("state") or "running"))
        semantic = project_semantic_run(
            run=run, events=events, tasks=tasks, artifacts=artifacts,
        )
        runs.append({
            "run_id": run_id,
            "session_id": session_id,
            "projection_cursor": projection_cursor(
                events, ledger_revision=run.get("projection_revision"),
            ),
            "workspace_id": str(run.get("workspace_id") or session.get("workspace_id") or ""),
            "mode": run.get("mode") or "default",
            "state": run.get("state") or "running",
            "stop_reason": run.get("stop_reason"),
            "started_at": run.get("started_at"),
            "updated_at": run.get("updated_at"),
            "ended_at": run.get("ended_at"),
            "budget": run.get("budget") or {},
            # ``runs.config_snapshot`` is captured, redacted, and made
            # immutable when the Run is created.  Carry that frozen record to
            # the Workbench so historical rows never inherit today's model
            # repository or runtime limits.
            "config_snapshot": run.get("config_snapshot") or {},
            "tasks": tasks,
            "artifacts": artifacts,
            "review": review,
            "semantic": semantic,
        })
    return {
        "schema": WORKBENCH_SCHEMA,
        "session_id": session_id,
        "workspace": workspace,
        "runs": runs,
    }


def build_workbench_run(session_id: str, run_id: str) -> dict[str, Any] | None:
    """Return a compact authoritative projection for one live run."""
    snapshot = build_workbench_snapshot(session_id)
    return next(
        (run for run in snapshot["runs"] if str(run.get("run_id")) == run_id),
        None,
    )


def public_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        key: task.get(key)
        for key in (
            "task_id", "run_id", "parent_task_id", "ordinal", "task_kind",
            "title", "description", "success_criteria", "actor_id",
            "actor_label", "assigned_model_id", "status", "attempt",
            "dependencies", "context_artifact_id", "result_artifact_id",
            "created_at", "updated_at",
        )
    }


def public_artifact_ref(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        key: artifact.get(key)
        for key in (
            "artifact_id", "run_id", "task_id", "kind", "title", "summary",
            "size_bytes", "created_at",
        )
    }


def projection_cursor(
    events: list[dict[str, Any]], *, ledger_revision: Any = 0,
) -> dict[str, int]:
    """Return the persisted event position represented by a Run projection.

    ``ledger_revision`` advances for every persisted Run/Task/Artifact/Event
    mutation. Sequence and event revision retain the exact typed-event cursor.
    The browser compares the ledger revision first, so a delayed snapshot whose
    event cursor did not move cannot roll task or artifact state backward.
    """
    sequence = max((_non_negative_int(event.get("sequence")) for event in events), default=0)
    event_revision = max(
        (
            _non_negative_int(event.get("revision"))
            for event in events
            if _non_negative_int(event.get("sequence")) == sequence
        ),
        default=0,
    )
    return {
        "ledger_revision": _non_negative_int(ledger_revision),
        "sequence": sequence,
        "event_revision": event_revision,
        # One-release alias for older Desktop bundles that still compare
        # ``sequence``/``revision`` as the typed-event cursor.
        "revision": event_revision,
    }


def build_run_review(events: list[dict[str, Any]], *, state: str = "running") -> dict[str, Any]:
    """Project audited mutation and verification events into review evidence.

    The Desktop previously rebuilt this card only from events observed by the
    current browser.  That made it disappear after a reconnect and allowed UI
    timing to decide which diff was visible.  This projection is deterministic
    from the persisted event ledger and contains no workspace file reads.
    """
    files: dict[str, dict[str, Any]] = {}
    verifications: list[dict[str, Any]] = []
    mutation_count = 0
    last_mutation_sequence = 0
    last_verification_sequence = 0
    for event in events:
        if str(event.get("type") or "") not in {"tool_result", "subagent_tool_result"}:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        path = str(metadata.get("path") or "").strip()
        if metadata.get("changed") is True and path:
            mutation_count += 1
            last_mutation_sequence = max(last_mutation_sequence, _non_negative_int(event.get("sequence")))
            file = files.setdefault(path, {
                "path": path,
                "operation": str(metadata.get("operation") or "edit"),
                "change_type": str(metadata.get("change_type") or "update"),
                "additions": 0,
                "deletions": 0,
                "diffs": [],
                "diff_truncated": False,
                "mutation_count": 0,
                "task_ids": [],
            })
            file["operation"] = str(metadata.get("operation") or file["operation"])
            if file["change_type"] != "create":
                file["change_type"] = str(metadata.get("change_type") or file["change_type"])
            file["additions"] += _non_negative_int(metadata.get("additions"))
            file["deletions"] += _non_negative_int(metadata.get("deletions"))
            file["mutation_count"] += 1
            diff = str(metadata.get("diff") or "").strip()
            if diff:
                file["diffs"].append(diff)
                file["diffs"] = file["diffs"][-4:]
            file["diff_truncated"] = bool(file["diff_truncated"] or metadata.get("diff_truncated"))
            task_id = str(event.get("task_id") or payload.get("task_id") or "")
            if task_id and task_id not in file["task_ids"]:
                file["task_ids"].append(task_id)

        verification = metadata.get("verification")
        parsed_result: dict[str, Any] | None = None
        if str(payload.get("name") or "") == "run_tests":
            try:
                candidate = json.loads(str(payload.get("result") or "{}"))
                if isinstance(candidate, dict) and candidate.get("schema") == "modus.verification.v1":
                    parsed_result = candidate
            except (TypeError, ValueError):
                parsed_result = None
        if isinstance(verification, dict) or parsed_result is not None:
            last_verification_sequence = max(last_verification_sequence, _non_negative_int(event.get("sequence")))
            merged = {**(parsed_result or {}), **(verification if isinstance(verification, dict) else {})}
            verifications.append({
                "schema": "modus.verification.v1",
                "status": str(merged.get("status") or "failed"),
                "command": str(merged.get("command") or ""),
                "path": str(merged.get("path") or ""),
                "exit_code": merged.get("exit_code"),
                "duration_seconds": merged.get("duration_seconds"),
                "counts": merged.get("counts") if isinstance(merged.get("counts"), dict) else {},
                "task_id": str(event.get("task_id") or payload.get("task_id") or "") or None,
            })

    public_files: list[dict[str, Any]] = []
    for file in files.values():
        diffs = file.pop("diffs")
        joined = "\n\n# 下一次变更\n".join(diffs)
        max_diff = 24_000
        if len(joined) > max_diff:
            joined = joined[:max_diff].rstrip() + "\n... [review diff truncated by Modus] ..."
            file["diff_truncated"] = True
        file["diff"] = joined
        public_files.append(file)

    additions = sum(int(file["additions"]) for file in public_files)
    deletions = sum(int(file["deletions"]) for file in public_files)
    latest_verification = verifications[-1] if verifications else None
    passing_is_current = (
        latest_verification is not None
        and latest_verification["status"] == "passed"
        and last_verification_sequence >= last_mutation_sequence
    )
    if passing_is_current:
        review_status = "verified"
    elif latest_verification and latest_verification["status"] != "passed":
        review_status = "failed"
    elif public_files and state in {"completed", "failed"}:
        review_status = "unverified"
    elif public_files:
        review_status = "changed"
    else:
        review_status = "clean"
    return {
        "schema": "modus.change-review.v1",
        "status": review_status,
        "run_state": state,
        "mutation_count": mutation_count,
        "file_count": len(public_files),
        "additions": additions,
        "deletions": deletions,
        "files": public_files,
        "verifications": verifications[-5:],
        "latest_verification": latest_verification,
    }


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
