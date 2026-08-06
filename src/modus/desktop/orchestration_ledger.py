"""Best-effort persistence helpers shared by MOA and Peri runners."""
from __future__ import annotations

import json
import logging
from typing import Any

from modus.desktop.artifacts import public_artifact, write_artifact
from modus.desktop.db import (
    add_memory_record,
    create_run_task,
    get_run,
    get_session,
    update_run_task,
)

logger = logging.getLogger(__name__)


def can_persist(session_id: str, run_id: str) -> bool:
    return bool(session_id and run_id and get_session(session_id) and get_run(run_id))


def persist_artifact(
    *, session_id: str, run_id: str, kind: str, title: str, content: str,
    task_id: str | None = None, summary: str = "",
) -> dict[str, Any] | None:
    if not can_persist(session_id, run_id):
        return None
    try:
        return write_artifact(
            session_id=session_id, run_id=run_id, task_id=task_id,
            kind=kind, title=title, content=content, summary=summary,
        )
    except Exception:
        logger.exception("could not persist orchestration artifact")
        return None


def persist_task(
    *, session_id: str, run_id: str, ordinal: int, task: dict[str, Any],
    assigned_model_id: str = "", context_artifact_id: str | None = None,
    parent_task_id: str | None = None, task_kind: str = "worker",
    actor_id: str = "", actor_label: str = "", depth: int = 0,
) -> dict[str, Any] | None:
    if not can_persist(session_id, run_id):
        return None
    try:
        return create_run_task(
            run_id=run_id, session_id=session_id, ordinal=ordinal,
            title=str(task.get("name") or f"子任务 {ordinal + 1}"),
            description=str(task.get("description") or ""),
            success_criteria=str(task.get("success_criteria") or ""),
            context_artifact_id=context_artifact_id,
            assigned_model_id=assigned_model_id,
            dependencies=list(task.get("dependencies") or []),
            parent_task_id=parent_task_id, task_kind=task_kind,
            actor_id=actor_id, actor_label=actor_label, depth=depth,
        )
    except Exception:
        logger.exception("could not persist orchestration task")
        return None


def set_task_state(
    task_id: str | None, status: str, *, result_artifact_id: str | None = None,
    increment_attempt: bool = False,
) -> None:
    if not task_id:
        return
    try:
        update_run_task(
            task_id, status=status, result_artifact_id=result_artifact_id,
            increment_attempt=increment_attempt,
        )
    except Exception:
        logger.exception("could not update orchestration task")


def persist_working_memory(
    *, session_id: str, run_id: str, content: str, category: str,
    source_ids: list[str] | None = None, task_id: str | None = None,
) -> dict[str, Any] | None:
    if not can_persist(session_id, run_id):
        return None
    try:
        return add_memory_record(
            session_id=session_id, run_id=run_id, task_id=task_id,
            scope="task" if task_id else "run", category=category,
            content=content, source_ids=source_ids, reference_only=True,
        )
    except Exception:
        logger.exception("could not persist orchestration memory")
        return None


def task_context_markdown(task: dict[str, Any], user_message: str) -> str:
    return (
        f"# {task.get('name') or 'Task'}\n\n"
        f"## Original user request\n\n{user_message}\n\n"
        f"## Assigned scope\n\n{task.get('description') or ''}\n\n"
        f"## Context\n\n{task.get('context') or ''}\n\n"
        f"## Success criteria\n\n{task.get('success_criteria') or ''}\n"
    )


def bounded_summary(text: str, limit: int = 4_000) -> str:
    """Deterministic head/tail context artifact; never invents a summary."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n\n[... {len(text) - limit} characters omitted ...]\n\n{text[-tail:]}"


def json_artifact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def artifact_event_payload(artifact: dict[str, Any] | None) -> dict[str, Any] | None:
    return public_artifact(artifact) if artifact else None
