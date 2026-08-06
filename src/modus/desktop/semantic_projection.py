"""Deterministic product-semantics projection for one persisted Agent run.

``AgentEvent`` remains the immutable fact ledger. This module derives the
smaller, user-facing vocabulary consumed by conversation and Workbench views:
outcome, evidence, recovery, and attention. It deliberately has no database,
transport, model, or DOM dependency so live updates and historical replay can
share the same semantics.
"""

from __future__ import annotations

import json
from typing import Any, Iterable


SEMANTIC_RUN_SCHEMA = "modus.semantic-run.v1"
VERIFICATION_SCHEMA = "modus.verification.v1"

_TOOL_RESULTS = frozenset({"tool_result", "subagent_tool_result"})
_TERMINAL_TYPES = frozenset({"run_completed", "run_error"})
_MUTATION_TOOLS = frozenset({"write_file", "edit_file", "patch"})
_BUDGET_STOPS = frozenset({"max_turns", "token_limit", "wall_time"})
_VERIFICATION_STOPS = frozenset({"verification_required", "verification_retry_limit"})

# Tool name -> short human action label for the activity feed.
_TOOL_ACTIONS = {
    "read_file": "读取文件",
    "write_file": "写入文件",
    "edit_file": "编辑文件",
    "patch": "应用补丁",
    "grep": "搜索文本",
    "search_code": "搜索代码",
    "search_memory": "检索记忆",
    "save_memory": "保存记忆",
    "bash": "执行命令",
    "run_tests": "运行测试",
    "list_dir": "浏览目录",
    "glob": "查找文件",
    "load_skill": "加载技能",
    "web_search": "网络搜索",
    "web_fetch": "抓取网页",
    "git_clone": "克隆仓库",
    "git_pull": "拉取更新",
    "git_push": "推送提交",
    "git_branch_create": "创建分支",
    "git_branch_checkout": "切换分支",
    "git_branch_merge": "合并分支",
    "git_remote_add": "添加远端",
    "git_credential_set": "保存凭据",
    "spawn_subtask": "拆分子任务",
}

# Tool name -> coarse activity category.  The board view groups these into
# "读 N / 写 M / 跑 K 命令" without string-matching localized labels.
_READ_TOOLS = frozenset({
    "read_file", "grep", "search_code", "search_memory", "list_dir", "glob",
    "load_skill", "web_search", "web_fetch",
})
_WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "patch", "save_memory",
    "git_clone", "git_pull", "git_push", "git_branch_create", "git_branch_checkout",
    "git_branch_merge", "git_remote_add", "git_credential_set", "spawn_subtask",
})
_COMMAND_TOOLS = frozenset({"bash", "run_tests"})

# Event type -> (activity kind, phase kind, default action).
_EVENT_KIND: dict[str, tuple[str, str | None, str]] = {
    "user_message": ("message", "analyzing", "用户输入"),
    "host_thinking": ("think", "analyzing", "分析"),
    "host_response": ("message", None, "回复"),
    "run_started": ("system", None, "运行开始"),
    "host_dispatch": ("dispatch", "analyzing", "分派任务"),
    "subtask_assignment": ("dispatch", "analyzing", "分配子任务"),
    "reference_started": ("reference", "analyzing", "参考分析开始"),
    "reference_response": ("reference", "reviewing", "参考回复"),
    "subagent_progress": ("subagent", "executing", "子任务进展"),
    "subagent_response": ("subagent", "reviewing", "子任务回复"),
    "host_review": ("review", "reviewing", "审阅结果"),
    "host_aggregation": ("aggregate", "executing", "聚合意见"),
    "approval_request": ("approval", "approving", "需要审批"),
    "approval_resolved": ("approval", None, "审批已处理"),
    "artifact": ("artifact", "delivering", "生成产物"),
    "context_compacted": ("system", None, "整理上下文"),
    "run_completed": ("system", "completed", "任务完成"),
    "run_error": ("system", "failed", "任务失败"),
}


def project_semantic_run(
    *,
    run: dict[str, Any],
    events: Iterable[dict[str, Any]],
    tasks: Iterable[dict[str, Any]] = (),
    artifacts: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Project authoritative run facts into ``modus.semantic-run.v1``.

    Duplicate and out-of-order snapshots are accepted. Stable event identity
    plus monotonic revision selects the authoritative snapshot; sequence then
    supplies deterministic run order.
    """
    ordered = _authoritative_events(events)
    task_rows = [dict(item) for item in tasks]
    artifact_rows = [dict(item) for item in artifacts]
    terminal = next(
        (event for event in reversed(ordered) if event.get("type") in _TERMINAL_TYPES),
        None,
    )

    evidence = _verification_evidence(ordered)
    last_mutation_sequence = _last_mutation_sequence(ordered)
    latest_verification = evidence[-1] if evidence else None
    current_verification = bool(
        latest_verification
        and latest_verification["status"] == "passed"
        and latest_verification["source_sequence"] >= last_mutation_sequence
    )
    recoveries = _recoveries(ordered, evidence)
    outcome = _outcome(
        run=run,
        terminal=terminal,
        evidence=evidence,
        current_verification=current_verification,
        last_mutation_sequence=last_mutation_sequence,
        recoveries=recoveries,
        pending_approval=_has_pending_approval(ordered),
    )
    activities = _activities(ordered, evidence)

    terminal_payload = _payload(terminal)
    budget = (
        terminal_payload.get("budget")
        if isinstance(terminal_payload.get("budget"), dict)
        else {}
    )
    run_id = str(run.get("run_id") or (terminal or {}).get("run_id") or "")
    return {
        "schema": SEMANTIC_RUN_SCHEMA,
        "run_id": run_id,
        "workspace_id": str(run.get("workspace_id") or ""),
        "goal": _goal(ordered, task_rows),
        "state": str(run.get("state") or _state_from_terminal(terminal)),
        "outcome": outcome,
        # Deterministic per-actor activity feed + collapsed process phases.
        # Both are mode-independent: default, MOA and Peri all produce the same
        # vocabulary, so board views render every run uniformly.
        "phases": _phases(activities),
        "activities": activities,
        "evidence": evidence,
        "recoveries": recoveries,
        "task_ids": [
            str(item.get("task_id")) for item in task_rows if item.get("task_id")
        ],
        "artifact_ids": [
            str(item.get("artifact_id"))
            for item in artifact_rows
            if item.get("artifact_id")
        ],
        "metrics": {
            "duration_seconds": _number(budget.get("elapsed_seconds")),
            "turns": _integer(
                terminal_payload.get("total_turns", budget.get("turns"))
            ),
            "tokens": _integer(
                terminal_payload.get("total_tokens", budget.get("total_tokens"))
            ),
        },
        "projection_cursor": _projection_cursor(ordered),
        "source_event_ids": [
            str(event.get("event_id")) for event in ordered if event.get("event_id")
        ],
    }


def _authoritative_events(
    events: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    for raw in events:
        if not isinstance(raw, dict):
            continue
        event = dict(raw)
        event_id = str(event.get("event_id") or "")
        if not event_id:
            anonymous.append(event)
            continue
        prior = by_id.get(event_id)
        if prior is None or _integer(event.get("revision")) >= _integer(
            prior.get("revision")
        ):
            by_id[event_id] = event
    selected = [*by_id.values(), *anonymous]
    return sorted(
        selected,
        key=lambda event: (
            _integer(event.get("sequence")),
            str(event.get("event_id") or ""),
        ),
    )


def _verification_evidence(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") not in _TOOL_RESULTS:
            continue
        payload = _payload(event)
        metadata = (
            payload.get("metadata")
            if isinstance(payload.get("metadata"), dict)
            else {}
        )
        verification = (
            metadata.get("verification")
            if isinstance(metadata.get("verification"), dict)
            else None
        )
        parsed: dict[str, Any] | None = None
        if str(payload.get("name") or "") == "run_tests":
            try:
                candidate = json.loads(str(payload.get("result") or "{}"))
            except (TypeError, ValueError):
                candidate = None
            if (
                isinstance(candidate, dict)
                and candidate.get("schema") == VERIFICATION_SCHEMA
            ):
                parsed = candidate
        if verification is None and parsed is None:
            continue
        merged = {**(parsed or {}), **(verification or {})}
        event_id = str(event.get("event_id") or "")
        result.append(
            {
                "evidence_id": (
                    f"evidence:{event_id or _integer(event.get('sequence'))}"
                ),
                "kind": str(merged.get("kind") or "tests"),
                "status": str(
                    merged.get("status")
                    or ("failed" if payload.get("is_error") else "passed")
                ),
                "command": str(merged.get("command") or ""),
                "path": str(merged.get("path") or ""),
                "exit_code": merged.get("exit_code"),
                "duration_seconds": _optional_number(
                    merged.get("duration_seconds")
                ),
                "counts": (
                    dict(merged.get("counts"))
                    if isinstance(merged.get("counts"), dict)
                    else {}
                ),
                "task_id": (
                    str(event.get("task_id") or payload.get("task_id") or "")
                    or None
                ),
                "source_event_id": event_id or None,
                "source_sequence": _integer(event.get("sequence")),
                "current": False,
            }
        )
    if result:
        last_mutation = _last_mutation_sequence(events)
        latest = result[-1]
        latest["current"] = bool(
            latest["status"] == "passed"
            and latest["source_sequence"] >= last_mutation
        )
    return result


def _last_mutation_sequence(events: list[dict[str, Any]]) -> int:
    latest = 0
    for event in events:
        if event.get("type") not in _TOOL_RESULTS:
            continue
        payload = _payload(event)
        metadata = (
            payload.get("metadata")
            if isinstance(payload.get("metadata"), dict)
            else {}
        )
        name = str(payload.get("name") or "")
        changed = metadata.get("changed") is True
        implicit_mutation = (
            name in _MUTATION_TOOLS and metadata.get("changed") is not False
        )
        if not payload.get("is_error") and (changed or implicit_mutation):
            latest = max(latest, _integer(event.get("sequence")))
    return latest


def _recoveries(
    events: list[dict[str, Any]], evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    recoveries: list[dict[str, Any]] = []

    failed_verifications: dict[str, dict[str, Any]] = {}
    for item in evidence:
        scope = str(item.get("task_id") or "root")
        if item["status"] != "passed":
            failed_verifications[scope] = item
            continue
        failed = failed_verifications.pop(scope, None)
        if failed is not None:
            recoveries.append(
                _recovery(
                    scope="verification",
                    failed_event_id=failed.get("source_event_id"),
                    recovered_event_id=item.get("source_event_id"),
                    task_id=item.get("task_id"),
                    summary="验证失败后重试通过",
                )
            )

    failed_tools: dict[tuple[str, str], dict[str, Any]] = {}
    verification_event_ids = {item.get("source_event_id") for item in evidence}
    for event in events:
        if (
            event.get("type") not in _TOOL_RESULTS
            or event.get("event_id") in verification_event_ids
        ):
            continue
        payload = _payload(event)
        name = str(payload.get("name") or "tool")
        task_id = str(event.get("task_id") or payload.get("task_id") or "")
        key = (task_id, name)
        if payload.get("is_error"):
            failed_tools[key] = event
            continue
        failed = failed_tools.pop(key, None)
        if failed is not None:
            recoveries.append(
                _recovery(
                    scope=f"tool:{name}",
                    failed_event_id=failed.get("event_id"),
                    recovered_event_id=event.get("event_id"),
                    task_id=task_id or None,
                    summary=f"{name} 失败后重试成功",
                )
            )
    return recoveries


def _recovery(
    *,
    scope: str,
    failed_event_id: Any,
    recovered_event_id: Any,
    task_id: Any,
    summary: str,
) -> dict[str, Any]:
    failed_id = str(failed_event_id or "")
    recovered_id = str(recovered_event_id or "")
    return {
        "recovery_id": f"recovery:{failed_id}:{recovered_id}",
        "scope": scope,
        "status": "recovered",
        "summary": summary,
        "task_id": str(task_id) if task_id else None,
        "failed_event_id": failed_id or None,
        "recovered_by_event_id": recovered_id or None,
    }


def _outcome(
    *,
    run: dict[str, Any],
    terminal: dict[str, Any] | None,
    evidence: list[dict[str, Any]],
    current_verification: bool,
    last_mutation_sequence: int,
    recoveries: list[dict[str, Any]],
    pending_approval: bool,
) -> dict[str, Any]:
    payload = _payload(terminal)
    stop_reason = str(
        payload.get("stop_reason") or run.get("stop_reason") or ""
    )
    event_type = str((terminal or {}).get("type") or "")
    event_status = str((terminal or {}).get("status") or "")
    run_state = str(run.get("state") or "running")

    if event_type == "run_completed" or (
        run_state == "completed" and event_type != "run_error"
    ):
        status = "succeeded"
        summary = (
            "任务已完成并通过验证" if current_verification else "任务已完成"
        )
    elif (
        stop_reason == "cancelled"
        or event_status == "cancelled"
        or run_state == "cancelled"
    ):
        status, summary = "cancelled", "任务已取消"
    elif stop_reason in _BUDGET_STOPS:
        status, summary = "incomplete", "任务因运行预算限制而停止"
    elif stop_reason in _VERIFICATION_STOPS:
        status, summary = "failed", "任务未获得有效验证"
    elif event_type == "run_error" or run_state in {"failed", "interrupted"}:
        status, summary = "failed", "任务未完成"
    else:
        status, summary = "running", "任务正在进行"

    has_mutation = last_mutation_sequence > 0
    if current_verification:
        confidence = "verified"
    elif has_mutation:
        confidence = "unverified"
    elif evidence and evidence[-1]["status"] != "passed":
        confidence = "failed_verification"
    else:
        confidence = "unverified"

    if status == "running" and pending_approval:
        attention = "action_required"
    elif status in {"failed", "incomplete"}:
        attention = "blocked"
    elif status == "cancelled":
        attention = "info"
    elif recoveries or (
        status == "succeeded" and has_mutation and not current_verification
    ):
        attention = "caution"
    else:
        attention = "none"

    return {
        "status": status,
        "summary": summary,
        "confidence": confidence,
        "attention": attention,
        "stop_reason": stop_reason or None,
        "verified": current_verification,
        "recovery_count": len(recoveries),
        "requires_user_action": attention == "action_required",
        "source_event_id": str((terminal or {}).get("event_id") or "") or None,
    }


def _has_pending_approval(events: list[dict[str, Any]]) -> bool:
    pending: set[str] = set()
    for event in events:
        payload = _payload(event)
        approval_id = str(payload.get("approval_id") or "")
        if event.get("type") == "approval_request" and approval_id:
            pending.add(approval_id)
        elif event.get("type") == "approval_resolved" and approval_id:
            pending.discard(approval_id)
    return bool(pending)


def _goal(
    events: list[dict[str, Any]], tasks: list[dict[str, Any]]
) -> dict[str, Any]:
    root = next(
        (item for item in tasks if item.get("task_kind") == "root"), None
    )
    if root and (root.get("title") or root.get("description")):
        return {
            "summary": str(root.get("title") or root.get("description")),
            "source": "task",
        }
    message = next(
        (event for event in events if event.get("type") == "user_message"), None
    )
    markdown = str(_payload(message).get("markdown") or "").strip()
    first_line = next(
        (line.strip() for line in markdown.splitlines() if line.strip()), ""
    )
    return {
        "summary": first_line[:240],
        "source": "user_message" if first_line else "unknown",
    }


def _activities(
    events: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project a deterministic, mode-independent activity feed.

    Every meaningful event becomes one activity carrying the owning actor
    (host / subagent / reference model / tool / user), a coarse kind, a short
    action label, and the sequence it happened at.  Evidence events are merged
    so a verification carries its pass/fail summary instead of a raw tool row.
    """
    evidence_by_sequence = {
        item.get("source_sequence"): item for item in evidence if item.get("source_sequence")
    }
    activities: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("type") or "")
        kind, phase_kind, default_action = _EVENT_KIND.get(
            event_type, ("tool", "executing", "工具操作")
        )
        if event_type in _TOOL_RESULTS:
            payload = _payload(event)
            tool_name = str(payload.get("name") or "tool")
            kind = "tool"
            phase_kind = "executing"
            default_action = _TOOL_ACTIONS.get(tool_name, tool_name)
        actor = _actor_label(event)
        sequence = _integer(event.get("sequence"))
        item: dict[str, Any] = {
            "activity_id": f"activity:{event.get('event_id') or sequence}",
            "actor": actor,
            "kind": kind,
            "phase": phase_kind,
            "action": default_action,
            "category": _category(event_type, _payload(event)),
            "detail": "",
            "status": _activity_status(event),
            "sequence": sequence,
            "source_event_id": str(event.get("event_id") or "") or None,
            "task_id": (
                str(event.get("task_id") or _payload(event).get("task_id") or "")
                or None
            ),
        }
        _enrich_activity(item, event, evidence_by_sequence)
        activities.append(item)
    return activities


def _category(event_type: str, payload: dict[str, Any]) -> str:
    """Machine-readable coarse category for board aggregation."""
    if event_type in _TOOL_RESULTS:
        name = str(payload.get("name") or "")
        if name in _READ_TOOLS:
            return "read"
        if name in _WRITE_TOOLS:
            return "write"
        if name in _COMMAND_TOOLS:
            return "command"
        return "tool"
    kind, _phase, _action = _EVENT_KIND.get(event_type, ("tool", None, ""))
    return kind


def _actor_label(event: dict[str, Any]) -> str:
    actor = event.get("actor")
    if isinstance(actor, dict):
        kind = str(actor.get("kind") or "")
        label = str(actor.get("label") or "")
        if label:
            return f"{kind}:{label}" if kind and kind not in {"host", "system"} else label
    if event.get("type") == "subagent_tool_call" or event.get("type") == "subagent_tool_result":
        return "worker"
    return "host"


def _activity_status(event: dict[str, Any]) -> str:
    status = str(event.get("status") or "completed")
    if status in {"completed", "streaming", "started"}:
        return "active"
    if status == "failed":
        return "error"
    if status == "cancelled":
        return "cancelled"
    return "active"


def _enrich_activity(
    item: dict[str, Any],
    event: dict[str, Any],
    evidence_by_sequence: dict[int, dict[str, Any]],
) -> None:
    """Attach the mode-specific detail string to a normalized activity."""
    event_type = str(event.get("type") or "")
    payload = _payload(event)
    if event_type in _TOOL_RESULTS:
        tool_name = str(payload.get("name") or "tool")
        meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        path = str(meta.get("path") or "") if meta.get("path") else ""
        if tool_name == "run_tests":
            item["kind"] = "verify"
            item["phase"] = "verifying"
            evidence_item = evidence_by_sequence.get(_integer(event.get("sequence")))
            if evidence_item:
                counts = evidence_item.get("counts") or {}
                passed = _integer(counts.get("passed"))
                failed = _integer(counts.get("failed"))
                item["detail"] = (
                    f"{passed} 通过 · {failed} 失败"
                    if passed or failed
                    else str(evidence_item.get("status") or "")
                )
                item["status"] = (
                    "ok" if evidence_item.get("status") == "passed" else "error"
                )
            else:
                item["status"] = "error" if payload.get("is_error") else "ok"
        elif path:
            item["detail"] = path
        elif tool_name == "bash":
            item["detail"] = str(payload.get("result") or "").splitlines()[0][:80]
            item["status"] = "error" if payload.get("is_error") else "ok"
        else:
            item["status"] = "error" if payload.get("is_error") else "ok"
    elif event_type in {"subagent_response", "reference_response", "host_review"}:
        markdown = str(payload.get("markdown") or "").strip()
        item["detail"] = next(
            (line.strip() for line in markdown.splitlines() if line.strip()), ""
        )[:120]
    elif event_type == "subtask_assignment":
        item["detail"] = str(payload.get("scope") or payload.get("title") or "")[:120]
    elif event_type == "artifact":
        item["detail"] = str(payload.get("title") or payload.get("kind") or "")[:80]
    elif event_type == "approval_request":
        item["detail"] = str(payload.get("tool_name") or payload.get("description") or "")[:80]
    elif event_type == "host_dispatch":
        item["detail"] = str(payload.get("target_label") or "")[:80]


def _phases(activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the activity feed into a short process-phase timeline.

    Consecutive activities sharing a phase collapse into one phase entry
    carrying its first/last sequence and an actor set.  This is the stable
    "which stage is this run in" backbone a board column can consume.
    """
    phases: list[dict[str, Any]] = []
    for activity in activities:
        phase = str(activity.get("phase") or "executing")
        if phases and phases[-1]["kind"] == phase:
            phases[-1]["end_sequence"] = activity["sequence"]
            actor = activity.get("actor")
            if actor and actor not in phases[-1]["actors"]:
                phases[-1]["actors"].append(actor)
            continue
        phases.append(
            {
                "kind": phase,
                "start_sequence": activity["sequence"],
                "end_sequence": activity["sequence"],
                "actors": [activity["actor"]] if activity.get("actor") else [],
                "count": 1,
            }
        )
    for phase in phases:
        phase["count"] = sum(
            1 for activity in activities
            if activity.get("phase") == phase["kind"]
            and _integer(activity.get("sequence")) <= _integer(phase["end_sequence"])
            and _integer(activity.get("sequence")) >= _integer(phase["start_sequence"])
        )
    return phases


def _projection_cursor(events: list[dict[str, Any]]) -> dict[str, int]:
    sequence = max(
        (_integer(event.get("sequence")) for event in events), default=0
    )
    revision = max(
        (
            _integer(event.get("revision"))
            for event in events
            if _integer(event.get("sequence")) == sequence
        ),
        default=0,
    )
    return {"sequence": sequence, "event_revision": revision}


def _state_from_terminal(terminal: dict[str, Any] | None) -> str:
    if not terminal:
        return "running"
    if terminal.get("type") == "run_completed":
        return "completed"
    return "cancelled" if terminal.get("status") == "cancelled" else "failed"


def _payload(event: dict[str, Any] | None) -> dict[str, Any]:
    if not event or not isinstance(event.get("payload"), dict):
        return {}
    return event["payload"]


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _number(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _optional_number(value: Any) -> float | None:
    if value is None:
        return None
    return _number(value)
