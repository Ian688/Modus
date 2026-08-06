from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from modus.desktop.events import Actor, ChannelId, EventStatus, EventType, RunEventEmitter
from modus.desktop.approval_flow import wait_for_user_approval
from modus.modes import PERI_MODE
from modus.runtime.controller import RunController
from modus.runtime.cancellation import RunCancelled, await_or_cancel
from modus.runtime.budget import BudgetExceeded, StopReason, bind_run_budget, reset_run_budget
from modus.runtime.state import RunState
from modus.tools.payload import artifact_ids_from_result

logger = logging.getLogger(__name__)


class _RunEventDeliveryError(Exception):
    """A worker callback could not deliver its typed event."""

    def __init__(self, message: str, cause: Exception) -> None:
        super().__init__(message)
        self.cause = cause


async def _cancel_and_reap(tasks: list[asyncio.Task[Any]]) -> None:
    """Cancel an entire fan-out and wait until no child can emit again."""
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _semantic_overlap(a: list[str] | None, b: list[str] | None) -> float:
    """Character 3-gram Jaccard similarity between two rounds of host outputs.

    Pure-Python proxy for semantic overlap without an embedding dependency.
    Equal or empty sides collapse to 1.0 (indistinguishable).
    """
    if not a or not b:
        return 1.0
    left: set[str] = set()
    right: set[str] = set()
    for text in a:
        for i in range(len(text) - 2):
            left.add(text[i:i + 3])
    for text in b:
        for i in range(len(text) - 2):
            right.add(text[i:i + 3])
    if not left or not right:
        return 1.0
    return len(left & right) / len(left | right)


async def run_peri_stream(
    websocket: WebSocket,
    session: Any,
    message: str,
    *,
    emitter: RunEventEmitter | None = None,
    controller: RunController | None = None,
    audit_event_for: Any = None,
    load_models: Any = None,
    reset_cancel: Any = None,
    persist_run_start: Any = None,
    skill_message: Any = None,
    context_messages: list[Message] | None = None,
    manage_controller: bool | None = None,
    wait_for_user_approval_callback: Any = None,
    attachments: list[dict[str, Any]] | None = None,
) -> RunEventEmitter:
    """Own one Peri consensus run lifecycle around the orchestration body."""
    emitter = emitter or RunEventEmitter(
        run_id=f"run_{uuid.uuid4().hex}", mode=PERI_MODE, send_json=websocket.send_json,
        audit_event=audit_event_for(session),
    )
    owns_controller = controller is None if manage_controller is None else manage_controller
    if controller is None:
        engine_config = getattr(getattr(session, "engine", None), "config", None)
        if engine_config is not None:
            controller = RunController.from_config(
                run_id=emitter.run_id, mode=PERI_MODE, config=engine_config,
            )
        else:
            controller = RunController(run_id=emitter.run_id, mode=PERI_MODE)
    if controller.state is RunState.CREATED:
        controller.transition(RunState.RUNNING)
    session.active_controller = controller
    if persist_run_start is not None:
        persisted = persist_run_start(session, emitter, controller, PERI_MODE)
        if (
            session.db_id
            and persisted is not None
            and (
                str((persisted or {}).get("run_id") or "") != emitter.run_id
                or not emitter.root_task_id
            )
        ):
            if not controller.is_terminal:
                controller.transition(RunState.FAILED)
            if session.active_controller is controller:
                session.active_controller = None
            raise RuntimeError("Run admission could not persist its root task")
    # Session owns its legacy cancellation token; the server injects the
    # compatibility callback rather than leaking a private session method.
    if reset_cancel is not None:
        reset_cancel().clear()
    completed_normally = False
    budget_token = bind_run_budget(controller.budget)
    try:
        await emitter.emit(
            EventType.RUN_STARTED, ChannelId.USER_HOST, Actor.system(),
            {"state": "running", "mode": PERI_MODE, "budget": controller.budget.snapshot()},
            status=EventStatus.STARTED,
        )
        workflow_completed = await _run_peri_body(
            websocket, session, message, emitter=emitter,
            controller=controller, load_models=load_models,
            skill_message=skill_message,
            context_messages=context_messages,
            wait_for_user_approval_callback=wait_for_user_approval_callback,
            attachments=attachments,
        )
        if controller.cancel_event.is_set():
            controller.budget.finish(StopReason.CANCELLED)
            await emitter.emit(
                EventType.RUN_ERROR, ChannelId.USER_HOST, Actor.system(),
                {
                    "code": "cancelled", "message": "用户中断", "retryable": False,
                    "stop_reason": "cancelled", "budget": controller.budget.snapshot(),
                },
                status=EventStatus.CANCELLED,
            )
            await websocket.send_json({
                "type": "done", "total_tokens": controller.budget.total_tokens,
                "total_turns": controller.budget.turns, "stop_reason": "cancelled",
                "budget": controller.budget.snapshot(),
            })
        elif workflow_completed:
            completed_normally = True
        elif not controller.is_terminal:
            controller.transition(RunState.FAILED)
    except BudgetExceeded as exc:
        controller.budget.finish(exc.reason)
        await emitter.emit(
            EventType.RUN_ERROR, ChannelId.USER_HOST, Actor.system(),
            {
                "code": "budget_exceeded", "message": f"Run stopped: {exc.reason.value}",
                "retryable": True, "stop_reason": exc.reason.value,
                "budget": controller.budget.snapshot(),
            },
            status=EventStatus.FAILED,
        )
        await websocket.send_json({
            "type": "done", "total_tokens": controller.budget.total_tokens,
            "total_turns": controller.budget.turns, "stop_reason": exc.reason.value,
            "budget": controller.budget.snapshot(),
        })
    except Exception as exc:
        disconnected = isinstance(exc, (RuntimeError, WebSocketDisconnect)) and (
            controller.cancel_event.is_set()
            or isinstance(exc, WebSocketDisconnect)
        )
        controller.budget.finish(
            StopReason.CANCELLED if disconnected else StopReason.FAILED,
        )
        if not controller.is_terminal:
            if disconnected:
                controller.cancel()
            else:
                controller.transition(RunState.FAILED)
        # Surface the underlying failure instead of a generic "orchestration
        # failed": the browser shows this to the user and it is the only place
        # the real cause (e.g. a bad prompt template) becomes visible.
        logger.warning("Peri run failed", exc_info=True)
        payload = {
            "code": "transport_disconnected" if disconnected else "peri_failed",
            "message": (
                "连接已断开，运行已取消。"
                if disconnected else "Peri orchestration failed"
            ),
            "detail": f"{type(exc).__name__}: {exc}",
            "retryable": True,
            "stop_reason": "cancelled" if disconnected else "failed",
            "budget": controller.budget.snapshot(),
        }
        try:
            await emitter.emit(
                EventType.RUN_ERROR, ChannelId.USER_HOST, Actor.system(), payload,
                status=(EventStatus.CANCELLED if disconnected else EventStatus.FAILED),
            )
            await websocket.send_json({
                "type": "done", "total_tokens": controller.budget.total_tokens,
                "total_turns": controller.budget.turns,
                "stop_reason": payload["stop_reason"],
                "budget": controller.budget.snapshot(),
            })
        except Exception:
            # The terminal audit precedes transport.  If delivery failed, the
            # caller's durable settlement guard will still see the event.
            pass
    finally:
        reset_run_budget(budget_token)
        if owns_controller:
            if controller.cancel_event.is_set():
                controller.cancel_complete()
            elif completed_normally and not controller.is_terminal:
                controller.transition(RunState.COMPLETED)
            elif not controller.is_terminal:
                controller.transition(RunState.FAILED)
            if session.active_controller is controller:
                session.active_controller = None
    return emitter


async def _run_peri_body(
    websocket: WebSocket,
    session: Any,
    message: str,
    *,
    emitter: RunEventEmitter,
    controller: RunController,
    load_models: Any,
    skill_message: Any = None,
    context_messages: list[Message] | None = None,
    wait_for_user_approval_callback: Any = None,
    attachments: list[dict[str, Any]] | None = None,
) -> bool:
    """Run the host-managed Peri workflow on the two typed channels.

    The upper channel is deliberately small: user intent and the host's final
    accountable answer.  The lower channel is the auditable management loop:
    task assignment, worker progress/results, and host review.  Workers only
    receive their assigned scope; the host owns the user relationship.
    """
    # Import through the compatibility implementation module at call time so
    # existing embedders that patch that boundary continue to work.
    from modus.desktop.peri import (
        PeriModelError,
        decompose_task,
        execute_subtask,
        review_subtask_outputs,
        merge_outputs,
        verify_subtask_criteria,
    )
    from modus.desktop.db import add_message
    from modus.desktop.orchestration_ledger import (
        artifact_event_payload, bounded_summary, json_artifact,
        persist_artifact, persist_task, persist_working_memory,
        set_task_state, task_context_markdown,
    )
    from modus.desktop.stopping import DecisionAction, ProgressCheckpoint, decide
    from modus.desktop.artifacts import read_artifact

    cancel_event = controller.cancel_event
    host = Actor.host("primary", "主持人")
    await emitter.emit(
        EventType.USER_MESSAGE, ChannelId.USER_HOST, Actor.user(),
        {"markdown": message, **({"attachments": attachments} if attachments else {})},
    )
    add_message(session.db_id, "user", message)
    from modus.agent.context import SessionContextProvider

    memory_context = SessionContextProvider().memory_text(session.db_id)
    orchestration_message = message + ("\n\n" + memory_context if memory_context else "")
    if skill_message is not None and getattr(skill_message, "content", ""):
        orchestration_message = f"{skill_message.content}\n\n{orchestration_message}"
    if context_messages:
        blocks = [
            m.content for m in context_messages
            if getattr(m, "content", "") and isinstance(getattr(m, "content", ""), str)
        ]
        if blocks:
            orchestration_message = "\n\n".join(blocks) + "\n\n" + orchestration_message

    models_data = load_models() if callable(load_models) else {}
    roles = models_data.get("peri_roles") if isinstance(models_data.get("peri_roles"), dict) else {}
    primary = roles.get("host") or {}
    sub_models = [roles[key] for key in ("worker_1", "worker_2") if key in roles]
    provider = primary.get("provider", "deepseek")
    model = primary.get("model", "deepseek-v4-flash")

    if not sub_models:
        error_text = "Peri 尚未配置协作模型。请在设置 → Peri 中至少选择一个 Worker。"
        await emitter.emit(
            EventType.RUN_ERROR, ChannelId.HOST_MODELS, Actor.system("Peri 配置"),
            {
                "code": "no_subagent_models", "message": error_text,
                "retryable": False, "stop_reason": "failed",
                "budget": controller.budget.snapshot(),
            },
            status=EventStatus.FAILED,
        )
        controller.budget.finish(StopReason.FAILED)
        await websocket.send_json({
            "type": "done", "total_tokens": 0, "total_turns": 0,
            "stop_reason": "failed", "budget": controller.budget.snapshot(),
        })
        return False

    try:
        subtasks = await await_or_cancel(decompose_task(
            orchestration_message, provider, model, len(sub_models), api_key=str(primary.get("api_key") or ""),
            base_url=primary.get("base_url") or None,
            temperature=float(primary.get("temperature", 0.4)),
            context_tokens=int(primary.get("context_tokens") or primary.get("context_window") or 128_000),
            reasoning_effort=primary.get("reasoning_effort"),
        ), cancel_event)
    except RunCancelled:
        return False
    except Exception:
        # A model or provider failure cannot silently become a successful
        # fallback task; the lifecycle wrapper records one failed terminal.
        raise

    analysis_artifact = persist_artifact(
        session_id=session.db_id, run_id=emitter.run_id,
        kind="task-analysis", title="Peri 任务分析",
        content=json_artifact({"user_request": message, "memory_context": memory_context, "subtasks": subtasks}),
        summary=f"主持人拆解为 {len(subtasks)} 个子任务",
    )
    if (payload := artifact_event_payload(analysis_artifact)) is not None:
        await emitter.emit(EventType.ARTIFACT, ChannelId.HOST_MODELS, host, payload)
    persist_working_memory(
        session_id=session.db_id, run_id=emitter.run_id,
        category="task-analysis", content=f"Host decomposed the run into {len(subtasks)} scoped tasks.",
        source_ids=[analysis_artifact["artifact_id"]] if analysis_artifact else [],
    )

    # Workers receive only the explicit read-only allowlist. MCP tools stay on
    # the Host until the protocol exposes trustworthy per-tool safety metadata.
    from modus.desktop.peri import build_subagent_tool_registry
    from modus.tools.builtins import get_builtin_tools

    # Feed the effective registry through the allowlist; unsupported tools are
    # intentionally discarded even when the Host can call them.
    all_tools = get_builtin_tools()
    engine_tools = getattr(getattr(session, "engine", None), "tool_registry", None)
    if engine_tools is not None:
        for name in engine_tools.list_names():
            tool = engine_tools.get(name)
            if tool is not None:
                all_tools.append(tool)
    # Writable mode is opt-in and approval-gated: each worker runs inside a
    # private worktree, writes there, and the Host approves the merge back.
    engine_config = getattr(getattr(session, "engine", None), "config", None)
    writable = bool(
        getattr(getattr(engine_config, "features", None), "writable_workers", False)
    )
    convergence_config = getattr(getattr(engine_config, "features", None), "convergence", None)
    max_recursion_depth = max(0, int(getattr(convergence_config, "max_recursion_depth", 0)))
    subagent_tools = build_subagent_tool_registry(
        all_tools, writable=writable, recursive=max_recursion_depth > 0,
    )
    convergence_enabled = bool(getattr(convergence_config, "enabled", True))
    max_revision_rounds = max(1, int(getattr(convergence_config, "max_revision_rounds", 3)))
    semantic_threshold = float(getattr(convergence_config, "semantic_threshold", 0.90))
    criteria_verification = bool(getattr(convergence_config, "criteria_verification", True))
    sprt_min_ratio = float(getattr(convergence_config, "sprt_min_ratio", 0.8))
    sprt_alpha = float(getattr(convergence_config, "sprt_alpha", 0.10))
    sprt_beta = float(getattr(convergence_config, "sprt_beta", 0.10))
    min_sprt_samples = max(2, int(getattr(convergence_config, "min_sprt_samples", 4)))
    # Collaboration runs require a workspace; the desktop WS layer enforces
    # that gate before admission (server ``workspace_required``).  This runtime
    # resolution keeps the explicit ``workspace_root`` when bound and otherwise
    # anchors to the engine cwd (which the engine now defaults to the home
    # directory), so a bare session can still orchestrate read-only workers.
    workspace_root = (
        str(Path(session.workspace_root).resolve())
        if getattr(session, "workspace_root", "")
        else str(Path(getattr(getattr(session, "engine", None), "cwd", Path.home())).resolve())
    )
    plan_id = f"run_{Path(emitter.run_id).name}" if emitter.run_id else "peri"
    data_root = str(
        Path(workspace_root) / ".modus-worktrees"
    )
    worktree_paths: dict[int, str] = {}
    if writable:
        from modus.desktop import worktree_orchestrator

        approval_fn = wait_for_user_approval_callback or wait_for_user_approval

        prepared = await worktree_orchestrator.prepare_worktrees(
            cwd=workspace_root, worker_count=len(sub_models),
            plan_id=plan_id, data_root=data_root,
            approval=lambda request: approval_fn(websocket, session, emitter, request),
        )
        if prepared["ok"]:
            for item in prepared["created"]:
                worktree_paths[int(item["ordinal"])] = item["path"]
            subagent_tools = build_subagent_tool_registry(
                all_tools, writable=True,
                recursive=max_recursion_depth > 0,
            )
        else:
            await emitter.emit(
                EventType.RUN_ERROR, ChannelId.HOST_MODELS, Actor.system("Peri 配置"),
                {
                    "code": "worktree_prepare_failed",
                    "message": str(prepared.get("error") or "无法准备可写 worktree"),
                    "retryable": False, "stop_reason": "failed",
                    "budget": controller.budget.snapshot(),
                },
                status=EventStatus.FAILED,
            )
            controller.budget.finish(StopReason.FAILED)
            await websocket.send_json({
                "type": "done", "total_tokens": controller.budget.total_tokens,
                "total_turns": controller.budget.turns, "stop_reason": "failed",
                "budget": controller.budget.snapshot(),
            })
            return False
    # Keep review evidence separate from clean worker output. The final merge
    # must integrate conclusions, not blindly expose internal tool transcripts.
    from modus.desktop.peri import _review_packet, build_revision_request

    worker_count = min(len(sub_models), len(subtasks))
    outputs: list[str] = [""] * worker_count
    review_outputs: list[str] = [""] * worker_count
    host_outputs: list[str] = [""] * worker_count
    evidence_by_task: list[list[dict]] = [[] for _ in range(worker_count)]
    assigned_tasks: list[dict] = []
    ledger_tasks: list[dict[str, Any] | None] = []
    worker_specs: list[dict[str, Any]] = []
    worker_states: list[str] = []
    tool_event_callbacks: list[Any] = []
    root_task_id = emitter.root_task_id

    for index, sub_model in enumerate(sub_models[:worker_count]):
        if cancel_event.is_set():
            return False
        task = subtasks[index]
        assigned_tasks.append(task)
        label = sub_model.get("name", f"子 LLM {index + 1}")
        worker = Actor.subagent(str(sub_model.get("id", index)), label)
        scope = task.get("description") or task.get("context") or message
        context_artifact = persist_artifact(
            session_id=session.db_id, run_id=emitter.run_id,
            kind="task-context", title=f"{task.get('name', f'子任务 {index + 1}')} · 上下文",
            content=task_context_markdown(task, orchestration_message), summary=str(scope)[:300],
        )
        ledger_task = persist_task(
            session_id=session.db_id, run_id=emitter.run_id, ordinal=index, task=task,
            assigned_model_id=str(sub_model.get("id") or ""),
            context_artifact_id=(context_artifact or {}).get("artifact_id"),
            parent_task_id=root_task_id, task_kind="worker",
            actor_id=str(sub_model.get("id") or index), actor_label=label,
        )
        ledger_tasks.append(ledger_task)
        worker_states.append("running")
        task_id = str((ledger_task or {}).get("task_id") or "") or f"task_{emitter.run_id}_{index + 1}"
        set_task_state(task_id, "running", increment_attempt=True)
        if (payload := artifact_event_payload(context_artifact)) is not None:
            await emitter.emit(
                EventType.ARTIFACT, ChannelId.HOST_MODELS, host, payload,
                task_id=task_id,
            )
        await emitter.emit(
            EventType.SUBTASK_ASSIGNMENT, ChannelId.HOST_MODELS, host,
            {"task_id": task_id, "target_id": str(sub_model.get("id") or index),
             "target_label": label, "title": task.get("name", f"子任务 {index + 1}"),
             "scope": scope, "success_criteria": task.get("success_criteria", "")},
        )
        await emitter.emit(
            EventType.SUBAGENT_PROGRESS, ChannelId.HOST_MODELS, worker,
            {"task_id": task_id, "text": "已接收任务，正在分析与执行。"}, status=EventStatus.STARTED,
        )
        started = await emitter.emit(
            EventType.SUBAGENT_RESPONSE, ChannelId.HOST_MODELS, worker,
            {"task_id": task_id, "markdown": ""}, status=EventStatus.STREAMING,
        )
        worker_event_id = started.event_id

        async def on_chunk(
            chunk: str, *, _worker: Actor = worker, _event_id: str = worker_event_id,
            _task_id: str = task_id,
        ) -> None:
            try:
                await emitter.emit(
                    EventType.SUBAGENT_RESPONSE, ChannelId.HOST_MODELS, _worker,
                    {"task_id": _task_id, "markdown": chunk}, status=EventStatus.STREAMING, event_id=_event_id,
                )
            except Exception as exc:
                raise _RunEventDeliveryError(
                    "Peri worker event delivery failed", exc,
                ) from exc

        tool_evidence = evidence_by_task[index]

        async def on_tool_event(
            raw: dict, *, _worker: Actor = worker, _evidence: list[dict] = tool_evidence,
            _task_id: str = task_id,
        ) -> None:
            kind = raw.get("type")
            tool_event_ids = getattr(on_tool_event, "_tool_event_ids", {})
            try:
                if kind == "subagent_tool_call":
                    payload = {
                        "task_id": _task_id, "name": raw.get("name", "工具"),
                        "input": raw.get("input", {}),
                    }
                    if raw.get("tool_call_id"):
                        payload["tool_call_id"] = str(raw["tool_call_id"])
                    call_event = await emitter.emit(
                        EventType.SUBAGENT_TOOL_CALL, ChannelId.HOST_MODELS, _worker, payload,
                        task_id=_task_id,
                    )
                    if raw.get("tool_call_id"):
                        tool_event_ids[str(raw["tool_call_id"])] = call_event.event_id
                        on_tool_event._tool_event_ids = tool_event_ids
                elif kind == "subagent_tool_result":
                    _evidence.append(dict(raw))
                    failed = bool(raw.get("is_error", False))
                    result_payload = {"task_id": _task_id, "name": raw.get("name", "工具"), "result": raw.get("result", ""),
                                      "is_error": failed}
                    if raw.get("display_summary"):
                        result_payload["display_summary"] = raw["display_summary"]
                    if raw.get("metadata"):
                        result_payload["metadata"] = raw["metadata"]
                    if raw.get("disclosure"):
                        result_payload["disclosure"] = raw["disclosure"]
                    if raw.get("tool_call_id"):
                        result_payload["tool_call_id"] = str(raw["tool_call_id"])
                    artifact_ids = artifact_ids_from_result(raw)
                    await emitter.emit(
                        EventType.SUBAGENT_TOOL_RESULT, ChannelId.HOST_MODELS, _worker, result_payload,
                        status=EventStatus.FAILED if failed else EventStatus.COMPLETED,
                        parent_event_id=tool_event_ids.get(str(raw.get("tool_call_id") or "")),
                        task_id=_task_id,
                        artifact_ids=artifact_ids,
                    )
            except Exception as exc:
                raise _RunEventDeliveryError(
                    "Peri worker tool event delivery failed", exc,
                ) from exc

        tool_event_callbacks.append(on_tool_event)
        worker_specs.append({
            "index": index, "task": task, "model": sub_model, "label": label,
            "worker": worker, "task_id": task_id, "event_id": worker_event_id,
            "context_artifact_id": (context_artifact or {}).get("artifact_id"),
            "on_chunk": on_chunk, "on_tool_event": on_tool_event,
        })

    async def run_worker(spec: dict[str, Any]) -> tuple[int, bool]:
        index = int(spec["index"])
        task, sub_model = spec["task"], spec["model"]
        label, worker, task_id = spec["label"], spec["worker"], spec["task_id"]
        try:
            context_artifact_id = str(spec.get("context_artifact_id") or "")
            if context_artifact_id:
                worker_context = read_artifact(
                    context_artifact_id, session_id=session.db_id,
                )
            else:                # Non-persisted embedders retain the in-memory compatibility
                # path; every persisted Desktop run uses the artifact above.
                worker_context = task_context_markdown(task, orchestration_message)
            sub_cwd = worktree_paths.get(index + 1) or workspace_root
            output = await await_or_cancel(execute_subtask(
                task, sub_model, worker_context, timeout=120.0,
                stream_callback=spec["on_chunk"], tool_registry=subagent_tools,
                event_callback=spec["on_tool_event"], cwd=sub_cwd,
                temperature=float(sub_model.get("temperature", 0.7)),
                context_tokens=int(sub_model.get("context_tokens") or sub_model.get("context_window") or 128_000),
                reasoning_effort=sub_model.get("reasoning_effort"),
                owner=f"worker_{index + 1}",
                writable=writable, cancel_event=cancel_event,
                max_recursion_depth=max_recursion_depth,
                session_id=session.db_id or None, run_id=emitter.run_id,
            ), cancel_event)
        except RunCancelled:
            worker_states[index] = "cancelled"
            set_task_state(task_id, "cancelled")
            raise
        except _RunEventDeliveryError:
            raise
        except Exception as exc:
            failure = f"[子任务失败: {str(exc)[:180]}]"
            outputs[index] = failure
            host_outputs[index] = failure
            review_outputs[index] = _review_packet(task, failure, evidence_by_task[index])
            worker_states[index] = "failed"
            set_task_state(task_id, "failed")
            await emitter.emit(
                EventType.SUBAGENT_RESPONSE, ChannelId.HOST_MODELS, worker,
                {"task_id": task_id, "markdown": failure}, status=EventStatus.FAILED, event_id=spec["event_id"],
            )
            return index, False

        outputs[index] = output
        result_artifact = persist_artifact(
            session_id=session.db_id, run_id=emitter.run_id, task_id=task_id,
            kind="worker-response", title=f"{label} · 完整回复", content=output,
            summary=bounded_summary(output, 500),
        )
        compressed_artifact = persist_artifact(
            session_id=session.db_id, run_id=emitter.run_id, task_id=task_id,
            kind="worker-summary", title=f"{label} · 压缩上下文",
            content=bounded_summary(output),
            summary=f"用于 Host 审阅的有界 Worker 输出（原文 {len(output)} 字符）",
        )
        summary_text = (
            read_artifact(compressed_artifact["artifact_id"], session_id=session.db_id)
            if compressed_artifact else bounded_summary(output)
        )
        host_outputs[index] = summary_text
        review_outputs[index] = _review_packet(
            task, summary_text, evidence_by_task[index],
        )
        worker_states[index] = "completed"
        set_task_state(task_id, "completed", result_artifact_id=(result_artifact or {}).get("artifact_id"))
        persist_working_memory(
            session_id=session.db_id, run_id=emitter.run_id, task_id=task_id,
            category="worker-result", content=bounded_summary(output, 1_500),
            source_ids=[item["artifact_id"] for item in (result_artifact, compressed_artifact) if item],
        )
        for artifact in (result_artifact, compressed_artifact):
            if (payload := artifact_event_payload(artifact)) is not None:
                await emitter.emit(
                    EventType.ARTIFACT, ChannelId.HOST_MODELS, worker, payload,
                    task_id=task_id,
                )
        await emitter.emit(
            EventType.SUBAGENT_RESPONSE, ChannelId.HOST_MODELS, worker,
            {"task_id": task_id}, status=EventStatus.COMPLETED, event_id=spec["event_id"],
        )
        return index, True

    def cancel_unsettled_worker_tasks() -> None:
        for index, state in enumerate(worker_states):
            if state == "running":
                worker_states[index] = "cancelled"
                ledger_task = ledger_tasks[index]
                task_id = str((ledger_task or {}).get("task_id") or "") or None
                set_task_state(task_id, "cancelled")

    worker_jobs = [asyncio.create_task(run_worker(spec)) for spec in worker_specs]
    successful_outputs = 0
    try:
        for completed in asyncio.as_completed(worker_jobs):
            _index, succeeded = await await_or_cancel(completed, cancel_event)
            successful_outputs += int(succeeded)
    except RunCancelled:
        await _cancel_and_reap(worker_jobs)
        cancel_unsettled_worker_tasks()
        return False
    except BaseException as exc:
        await _cancel_and_reap(worker_jobs)
        cancel_unsettled_worker_tasks()
        if isinstance(exc, _RunEventDeliveryError):
            raise exc.cause
        raise

    if successful_outputs != worker_count:
        raise PeriModelError(
            f"Only {successful_outputs} of {worker_count} Peri workers completed"
        )

    worker_checkpoint = ProgressCheckpoint(
        completed_required=successful_outputs,
        total_required=worker_count,
        verified_criteria=0,
        total_criteria=worker_count,
        new_evidence=sum(len(items) for items in evidence_by_task),
        unresolved_dependencies=sum(len(task.get("dependencies") or []) for task in assigned_tasks),
    )
    worker_decision = decide([worker_checkpoint])
    decision_artifact = persist_artifact(
        session_id=session.db_id, run_id=emitter.run_id,
        kind="stop-decision", title="Peri 进度决策",
        content=json_artifact(worker_decision.to_wire()),
        summary=worker_decision.reason,
    )
    persist_working_memory(
        session_id=session.db_id, run_id=emitter.run_id,
        category="stop-decision", content=json_artifact(worker_decision.to_wire()),
        source_ids=[decision_artifact["artifact_id"]] if decision_artifact else [],
    )
    if (payload := artifact_event_payload(decision_artifact)) is not None:
        await emitter.emit(EventType.ARTIFACT, ChannelId.HOST_MODELS, Actor.system("调度器"), payload)

    review_markdown = "主持人已审阅子任务输出：结果满足当前任务要求。"
    all_ok = False
    guidance: list[str] = []
    review_checkpoints: list[ProgressCheckpoint] = []
    previous_mean: float | None = None
    previous_outputs: list[str] | None = None
    converged = False
    revision_round = 0
    while True:
        all_ok, guidance, scores = await await_or_cancel(review_subtask_outputs(
            assigned_tasks, review_outputs, provider, model, orchestration_message,
            api_key=str(primary.get("api_key") or ""), base_url=primary.get("base_url") or None,
            temperature=float(primary.get("temperature", 0.4)),
            context_tokens=int(primary.get("context_tokens") or primary.get("context_window") or 128_000),
            reasoning_effort=primary.get("reasoning_effort"),
        ), cancel_event)
        mean_score = (sum(scores) / len(scores)) if scores else 0.0
        score_delta = (mean_score - previous_mean) if previous_mean is not None else 0.0
        overlap = _semantic_overlap(previous_outputs, host_outputs)
        # Objective signal: Host judges each worker's success_criteria checklist
        # item-by-item.  The satisfied count replaces the subjective 0-10 score
        # as the SPRT input.
        criteria_counts: list[dict] = []
        criteria_verified = 0
        criteria_total = 0
        if criteria_verification:
            for index in range(worker_count):
                try:
                    count = await await_or_cancel(verify_subtask_criteria(
                        assigned_tasks[index], host_outputs[index],
                        provider, model,
                        api_key=str(primary.get("api_key") or ""),
                        base_url=primary.get("base_url") or None,
                        temperature=float(primary.get("temperature", 0.4)),
                        context_tokens=int(primary.get("context_tokens") or primary.get("context_window") or 128_000),
                        reasoning_effort=primary.get("reasoning_effort"),
                    ), cancel_event)
                except RunCancelled:
                    return False
                except Exception:
                    # A verification failure is not a silent pass; fall back to
                    # the Host's all_acceptable so the run cannot claim criteria
                    # were met when they could not be verified.
                    count = {"verified": 0, "total": 0, "verdicts": []}
                criteria_counts.append(count)
                criteria_verified += int(count.get("verified") or 0)
                criteria_total += int(count.get("total") or 0)
        verified_count = criteria_verified if criteria_total else (
            worker_count if all_ok else max(0, worker_count - len(guidance))
        )
        total_count = criteria_total if criteria_total else worker_count
        criteria_all_met = criteria_total > 0 and criteria_verified >= criteria_total
        review_checkpoint = ProgressCheckpoint(
            completed_required=successful_outputs,
            total_required=worker_count,
            verified_criteria=verified_count,
            total_criteria=total_count,
            new_evidence=sum(len(items) for items in evidence_by_task),
            verification_delta=(verified_count / total_count) if total_count else 0.0,
            unresolved_dependencies=0,
            revision_failures=sum(1 for output in outputs if output.startswith("[子任务失败:")),
            mean_score=mean_score,
            score_delta=score_delta,
            semantic_overlap=overlap,
        )
        review_checkpoints.append(review_checkpoint)
        review_decision = decide(
            [worker_checkpoint, *review_checkpoints],
            semantic_threshold=semantic_threshold,
            sprt_alpha=sprt_alpha, sprt_beta=sprt_beta,
            min_sprt_samples=min_sprt_samples, sprt_min_ratio=sprt_min_ratio,
        )
        decision_payload = dict(review_decision.to_wire())
        if criteria_counts:
            decision_payload["criteria"] = {
                "verified": criteria_verified,
                "total": criteria_total,
                "per_worker": [
                    {"verified": c.get("verified") or 0, "total": c.get("total") or 0}
                    for c in criteria_counts
                ],
            }
        review_decision_artifact = persist_artifact(
            session_id=session.db_id, run_id=emitter.run_id,
            kind="stop-decision", title="Peri 审阅后决策",
            content=json_artifact(decision_payload),
            summary=review_decision.reason,
        )
        if (payload := artifact_event_payload(review_decision_artifact)) is not None:
            await emitter.emit(EventType.ARTIFACT, ChannelId.HOST_MODELS, Actor.system("调度器"), payload)

        if all_ok or criteria_all_met:
            review_markdown = "主持人已审阅子任务输出：结果满足当前任务要求。"
            break
        if review_decision.action in {
            DecisionAction.STOP_SUCCESS, DecisionAction.STOP_CONVERGED,
        }:
            converged = True
            review_markdown = (
                "主持人判定修订已收敛，接受当前共识结论。"
                f"\n\n{review_decision.reason}"
            )
            break
        if not guidance:
            raise PeriModelError("Host rejected the worker outputs without revision guidance")
        if (
            review_decision.action in {
                DecisionAction.STOP_STALLED, DecisionAction.REDECOMPOSE,
                DecisionAction.ARBITRATE,
            }
            or not convergence_enabled
            or revision_round >= max_revision_rounds
        ):
            detail = "; ".join(guidance[:3]) or "no actionable guidance"
            raise PeriModelError(f"Revised outputs did not pass Host review: {detail}")
        review_markdown = "主持人要求修订：\n" + "\n".join(f"- {item}" for item in guidance)
        # Keep every correction visible as a fresh lower-channel worker response.
        for index, guidance_item in enumerate(guidance[:len(sub_models)]):
            if cancel_event.is_set():
                return False
            revision_request = build_revision_request(
                guidance_item, outputs[index],
                evidence_by_task[index] if index < len(evidence_by_task) else [],
            )
            try:
                    revision_context_artifact = persist_artifact(
                        session_id=session.db_id, run_id=emitter.run_id,
                        task_id=str((ledger_tasks[index] or {}).get("task_id") or "") or None,
                        kind="revision-context", title=f"子任务 {index + 1} · 修订上下文",
                        content=(
                            task_context_markdown(assigned_tasks[index], orchestration_message)
                            + "\n\n## Host revision request\n\n" + revision_request
                        ),
                        summary=bounded_summary(guidance_item, 300),
                    )
                    if (payload := artifact_event_payload(revision_context_artifact)) is not None:
                        await emitter.emit(EventType.ARTIFACT, ChannelId.HOST_MODELS, host, payload)
                    revision_context = (
                        read_artifact(
                            revision_context_artifact["artifact_id"], session_id=session.db_id,
                        ) if revision_context_artifact else revision_request
                    )
                    sub_cwd = worktree_paths.get(index + 1) or workspace_root
                    revised = await await_or_cancel(execute_subtask(
                        assigned_tasks[index], sub_models[index],
                        revision_context, timeout=120.0,
                        tool_registry=subagent_tools,
                        event_callback=tool_event_callbacks[index],
                        cwd=sub_cwd,
                        temperature=float(sub_models[index].get("temperature", 0.7)),
                        context_tokens=int(sub_models[index].get("context_tokens") or sub_models[index].get("context_window") or 128_000),
                        reasoning_effort=sub_models[index].get("reasoning_effort"),
                        owner=f"worker_{index + 1}",
                        writable=writable, cancel_event=cancel_event,
                        max_recursion_depth=max_recursion_depth,
                        session_id=session.db_id or None, run_id=emitter.run_id,
                    ), cancel_event)
                    if revised:
                        outputs[index] = revised
                        ledger_task = ledger_tasks[index] if index < len(ledger_tasks) else None
                        revision_task_id = str((ledger_task or {}).get("task_id") or "") or None
                        revision_artifact = persist_artifact(
                            session_id=session.db_id, run_id=emitter.run_id,
                            task_id=revision_task_id, kind="worker-revision",
                            title=f"子任务 {index + 1} · 修订回复", content=revised,
                            summary=bounded_summary(revised, 500),
                        )
                        revision_summary_artifact = persist_artifact(
                            session_id=session.db_id, run_id=emitter.run_id,
                            task_id=revision_task_id, kind="worker-summary",
                            title=f"子任务 {index + 1} · 修订压缩上下文",
                            content=bounded_summary(revised),
                            summary=f"用于 Host 审阅的有界修订输出（原文 {len(revised)} 字符）",
                        )
                        set_task_state(
                            revision_task_id, "completed",
                            result_artifact_id=(revision_artifact or {}).get("artifact_id"),
                            increment_attempt=True,
                        )
                        persist_working_memory(
                            session_id=session.db_id, run_id=emitter.run_id,
                            category="worker-revision",
                            content=(
                                f"Worker {index + 1} 修订完成（第 {revision_round} 轮）："
                                f"{bounded_summary(revised, 1_500)}"
                            ),
                            source_ids=[revision_artifact["artifact_id"]] if revision_artifact else None,
                        )
                        for artifact in (revision_artifact, revision_summary_artifact):
                            if (payload := artifact_event_payload(artifact)) is not None:
                                await emitter.emit(
                                    EventType.ARTIFACT, ChannelId.HOST_MODELS,
                                    Actor.subagent(str(sub_models[index].get("id", index)), sub_models[index].get("name", f"子 LLM {index + 1}")),
                                    payload,
                                )
                        if index < len(review_outputs):
                            host_outputs[index] = (
                                read_artifact(
                                    revision_summary_artifact["artifact_id"], session_id=session.db_id,
                                ) if revision_summary_artifact else bounded_summary(revised)
                            )
                            review_outputs[index] = _review_packet(
                                assigned_tasks[index], host_outputs[index],
                                evidence_by_task[index] if index < len(evidence_by_task) else [],
                            )
                            await emitter.emit(
                                EventType.SUBAGENT_RESPONSE, ChannelId.HOST_MODELS,
                                Actor.subagent(str(sub_models[index].get("id", index)), sub_models[index].get("name", f"子 LLM {index + 1}")),
                                {"task_id": revision_task_id, "markdown": revised, "revision": True}, status=EventStatus.COMPLETED,
                            )
            except RunCancelled:
                return False
            except Exception as exc:
                raise PeriModelError(
                    f"Worker {index + 1} revision failed: {str(exc)[:120]}"
                ) from exc
        previous_mean = mean_score
        previous_outputs = host_outputs[:]
        revision_round += 1
        review_markdown = f"主持人要求第 {revision_round} 轮修订。"
    if not converged:
        review_markdown += "\n\n主持人复审通过，修订结果满足当前任务要求。"
    else:
        review_markdown += "\n\n主持人判定修订收敛，接受当前共识。"
    if criteria_total:
        review_markdown += (
            f"\n\n成功标准逐条验证：满足 {criteria_verified}/{criteria_total} 项。"
        )
    await emitter.emit(EventType.HOST_REVIEW, ChannelId.HOST_MODELS, host, {"markdown": review_markdown})
    review_artifact = persist_artifact(
        session_id=session.db_id, run_id=emitter.run_id,
        kind="host-review", title="Host 审阅结论", content=review_markdown,
        summary=bounded_summary(review_markdown, 500),
    )
    if (payload := artifact_event_payload(review_artifact)) is not None:
        await emitter.emit(EventType.ARTIFACT, ChannelId.HOST_MODELS, host, payload)
    try:
        final = await await_or_cancel(merge_outputs(
            assigned_tasks, host_outputs, provider, model, orchestration_message,
            api_key=str(primary.get("api_key") or ""), base_url=primary.get("base_url") or None,
            temperature=float(primary.get("temperature", 0.4)),
            context_tokens=int(primary.get("context_tokens") or primary.get("context_window") or 128_000),
            reasoning_effort=primary.get("reasoning_effort"),
        ), cancel_event)
    except RunCancelled:
        return False
    # Emit host response to main chat
    await emitter.emit(EventType.HOST_RESPONSE, ChannelId.USER_HOST, host, {"markdown": final, "streaming": True})
    add_message(session.db_id, "assistant", final)
    final_artifact = persist_artifact(
        session_id=session.db_id, run_id=emitter.run_id,
        kind="host-final", title="Peri 共识结论", content=final,
        summary=bounded_summary(final, 500),
    )
    if (payload := artifact_event_payload(final_artifact)) is not None:
        await emitter.emit(EventType.ARTIFACT, ChannelId.HOST_MODELS, host, payload)
    controller.budget.finish(StopReason.COMPLETED)
    terminal = {
        "total_tokens": controller.budget.total_tokens,
        "total_turns": controller.budget.turns,
        "stop_reason": "completed", "budget": controller.budget.snapshot(),
    }
    await emitter.emit(EventType.RUN_COMPLETED, ChannelId.USER_HOST, host, terminal)
    await websocket.send_json({"type": "done", **terminal})

    # Writable workers: merge each branch back behind the merge_changes gate,
    # then clean up. The consensus answer above is already final and the run
    # terminal; a merge failure must not re-open a completed run, so it is
    # surfaced as a reviewable worktree artifact rather than a terminal event.
    if writable and worktree_paths:
        from modus.desktop import worktree_orchestrator

        for ordinal in sorted(worktree_paths):
            if controller.cancel_event.is_set():
                break
            merged = await worktree_orchestrator.merge_worktree(
                cwd=workspace_root, plan_id=plan_id, data_root=data_root,
                ordinal=ordinal,
                approval=lambda request: approval_fn(websocket, session, emitter, request),
            )
            if not merged["ok"]:
                merge_artifact = persist_artifact(
                    session_id=session.db_id, run_id=emitter.run_id,
                    kind="worktree-merge", title=f"Worker {ordinal} · 合并失败",
                    content=json_artifact({
                        "merged": False,
                        "error": str(merged.get("error") or "unknown"),
                    }),
                    summary=f"Worker {ordinal} 的改动未能合并回主分支",
                )
            else:
                merge_artifact = persist_artifact(
                    session_id=session.db_id, run_id=emitter.run_id,
                    kind="worktree-merge", title=f"Worker {ordinal} · 合并回主干",
                    content=json_artifact({"branch": merged.get("branch"), "merged": True}),
                    summary=f"Worker {ordinal} 的改动已合并回主分支",
                )
                await worktree_orchestrator.cleanup_worktree(
                    cwd=workspace_root, plan_id=plan_id, data_root=data_root, ordinal=ordinal,
                )
            if (payload := artifact_event_payload(merge_artifact)) is not None:
                await emitter.emit(
                    EventType.ARTIFACT, ChannelId.HOST_MODELS, Actor.system("调度器"), payload,
                )
    return True
