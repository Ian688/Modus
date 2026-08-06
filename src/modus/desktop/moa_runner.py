"""MOA runner: references advise → aggregator synthesizes guidance → host runs with tools.

Unlike the old implementation where host responded directly, the new flow:
1. Reference models advise (no tools)
2. Aggregator synthesizes guidance
3. The guidance is returned to the server, which then runs the default Agent
   (with full tool access) using the guidance as context.

This matches Hermes' MOA pattern: MOA is an enhancement layer for the
normal agent loop, not a replacement.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from fastapi import WebSocket

from modus.desktop.events import Actor, ChannelId, EventStatus, EventType, RunEventEmitter
from modus.runtime.controller import RunController
from modus.runtime.cancellation import RunCancelled, await_or_cancel
from modus.runtime.budget import BudgetExceeded, StopReason, bind_run_budget, reset_run_budget
from modus.runtime.state import RunState
from modus.types import Message


class _RunEventDeliveryError(Exception):
    """A reference callback could not deliver its typed event."""

    def __init__(self, message: str, cause: Exception) -> None:
        super().__init__(message)
        self.cause = cause


async def _cancel_and_reap(tasks: list[asyncio.Task[Any]]) -> None:
    """Cancel an entire fan-out and wait until no child can emit again."""
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def run_moa_stream(
    websocket: WebSocket,
    session: Any,
    messages: list[Message],
    *,
    emitter: RunEventEmitter | None = None,
    controller: RunController | None = None,
    audit_event_for: Any,
    load_models: Any,
) -> str:
    """Run MOA: references → aggregator → return guidance for the default Agent.

    Returns the aggregator's synthesized guidance text, or empty string if MOA
    couldn't complete. The caller (server) should pass this guidance to the
    default Agent runner so the host can continue with tool access.
    """
    from modus.agent.moa import call_aggregator, call_reference
    from modus.desktop.orchestration_ledger import (
        artifact_event_payload, bounded_summary, persist_artifact,
        persist_task, persist_working_memory, set_task_state,
    )

    emitter = emitter or RunEventEmitter(
        run_id=f"run_{uuid.uuid4().hex}", mode="moa", send_json=websocket.send_json,
        audit_event=audit_event_for(session),
    )
    owns_controller = controller is None
    controller = controller or RunController(run_id=emitter.run_id, mode="moa")
    if controller.state is RunState.CREATED:
        controller.transition(RunState.RUNNING)
    session.active_controller = controller
    legacy_cancel_event = session._ensure_cancel()
    legacy_cancel_event.clear()
    host = Actor.host("primary", "主持人")

    # ── Load model configs ──
    data = load_models() if callable(load_models) else {}
    roles = data.get("moa_roles") if isinstance(data.get("moa_roles"), dict) else {}
    host_config = roles.get("host")
    ref_configs = [roles[key] for key in ("reference_1", "reference_2") if key in roles]

    if not host_config:
        await emitter.emit(
            EventType.RUN_ERROR, ChannelId.HOST_MODELS, Actor.system("MOA 配置"),
            {
                "code": "no_host_model", "message": "MOA 未配置主持人模型。",
                "retryable": False, "stop_reason": "failed",
                "budget": controller.budget.snapshot(),
            },
            status=EventStatus.FAILED,
        )
        controller.budget.finish(StopReason.FAILED)
        if not controller.is_terminal:
            controller.transition(RunState.FAILED)
        if owns_controller:
            if session.active_controller is controller:
                session.active_controller = None
        return ""

    start_time = time.time()
    system_prompt = getattr(session, "system_prompt", "") or "You are a helpful assistant."
    guidance = ""
    budget_token = bind_run_budget(controller.budget)
    reference_task_ids: dict[int, str] = {}
    reference_states: dict[int, str] = {}

    try:
      # ── Phase 1: Reference models advise ──
      if ref_configs:
        await emitter.emit(
            EventType.HOST_AGGREGATION, ChannelId.HOST_MODELS, host,
            {"summary": f"🧠 征询 {len(ref_configs)} 个参考模型意见…", "time_ms": 0},
        )

        for idx, ref in enumerate(ref_configs):
            label = ref.get("name") or f"{ref.get('provider', '')}/{ref.get('model', '')}"
            ref_actor = Actor.reference_model(str(ref.get("id", idx)), label)
            ledger_task = persist_task(
                session_id=session.db_id, run_id=emitter.run_id, ordinal=idx,
                task={
                    "name": f"{label} · 参考分析",
                    "description": "独立分析当前对话并提供参考意见。",
                    "success_criteria": "提供可供主持人综合的独立建议。",
                },
                assigned_model_id=str(ref.get("id") or ""),
                parent_task_id=emitter.root_task_id, task_kind="reference",
                actor_id=str(ref.get("id") or idx), actor_label=label,
            )
            task_id = str((ledger_task or {}).get("task_id") or f"task_{emitter.run_id}_ref_{idx}")
            reference_task_ids[idx] = task_id
            reference_states[idx] = "running"
            set_task_state(task_id, "running", increment_attempt=True)
            await emitter.emit(
                EventType.HOST_DISPATCH, ChannelId.HOST_MODELS, host,
                {"task_id": task_id, "target_id": str(ref.get("id", idx)), "target_label": label,
                 "request_markdown": "请分析当前对话状态并提供独立参考意见。"},
            )
            await emitter.emit(
                EventType.REFERENCE_STARTED, ChannelId.HOST_MODELS, ref_actor,
                {"task_id": task_id, "target_id": str(ref.get("id", idx)), "request_markdown": ""}, status=EventStatus.STARTED,
            )

        async def call_one(idx: int, ref: dict) -> tuple[int, str, str]:
            label = ref.get("name") or f"{ref.get('provider', '')}/{ref.get('model', '')}"
            ref_actor = Actor.reference_model(str(ref.get("id", idx)), label)
            task_id = reference_task_ids[idx]
            started = await emitter.emit(
                EventType.REFERENCE_RESPONSE, ChannelId.HOST_MODELS, ref_actor,
                {"task_id": task_id, "target_id": str(ref.get("id", idx)), "markdown": "", "time_ms": 0}, status=EventStatus.STREAMING,
            )
            ref_event_id = started.event_id

            async def on_chunk(chunk: str) -> None:
                try:
                    await emitter.emit(
                        EventType.REFERENCE_RESPONSE, ChannelId.HOST_MODELS, ref_actor,
                        {"task_id": task_id, "target_id": str(ref.get("id", idx)), "markdown": chunk, "time_ms": int((time.time() - start_time) * 1000)},
                        status=EventStatus.STREAMING, event_id=ref_event_id,
                    )
                except Exception as exc:
                    raise _RunEventDeliveryError(
                        "MOA reference event delivery failed", exc,
                    ) from exc

            try:
                output = await await_or_cancel(call_reference(
                    ref, messages, system_prompt,
                    temperature=float(ref.get("temperature", 0.7)), timeout=30.0,
                    stream_callback=on_chunk, owner=f"reference_{idx + 1}",
                ), controller.cancel_event)
            except RunCancelled:
                reference_states[idx] = "cancelled"
                set_task_state(task_id, "cancelled")
                await emitter.emit(
                    EventType.REFERENCE_RESPONSE, ChannelId.HOST_MODELS, ref_actor,
                    {"task_id": task_id, "target_id": str(ref.get("id", idx)), "time_ms": int((time.time() - start_time) * 1000)},
                    status=EventStatus.CANCELLED, event_id=ref_event_id,
                )
                raise
            except _RunEventDeliveryError:
                raise
            except Exception as exc:
                reference_states[idx] = "failed"
                set_task_state(task_id, "failed")
                await emitter.emit(
                    EventType.REFERENCE_RESPONSE, ChannelId.HOST_MODELS,
                    Actor.reference_model(str(ref.get("id", idx)), label),
                    {"task_id": task_id, "target_id": str(ref.get("id", idx)), "time_ms": int((time.time() - start_time) * 1000)},
                    status=EventStatus.FAILED, event_id=ref_event_id,
                )
                return idx, label, f"[调用失败: {exc}]"

            await emitter.emit(
                EventType.REFERENCE_RESPONSE, ChannelId.HOST_MODELS,
                Actor.reference_model(str(ref.get("id", idx)), label),
                {"task_id": task_id, "target_id": str(ref.get("id", idx)), "time_ms": int((time.time() - start_time) * 1000)},
                status=EventStatus.COMPLETED, event_id=ref_event_id,
            )
            artifact = persist_artifact(
                session_id=session.db_id, run_id=emitter.run_id,
                task_id=task_id,
                kind="moa-reference", title=f"{label} · 参考意见", content=output,
                summary=bounded_summary(output, 500),
            )
            persist_working_memory(
                session_id=session.db_id, run_id=emitter.run_id,
                category="reference-advice", content=bounded_summary(output, 1_500),
                source_ids=[artifact["artifact_id"]] if artifact else [],
            )
            if (payload := artifact_event_payload(artifact)) is not None:
                await emitter.emit(
                    EventType.ARTIFACT, ChannelId.HOST_MODELS, ref_actor,
                    payload, task_id=task_id,
                )
            reference_states[idx] = "completed"
            set_task_state(
                task_id, "completed",
                result_artifact_id=(artifact or {}).get("artifact_id"),
            )
            return idx, label, output

        def cancel_unsettled_reference_tasks() -> None:
            for idx, state in reference_states.items():
                if state == "running":
                    reference_states[idx] = "cancelled"
                    set_task_state(reference_task_ids[idx], "cancelled")

        tasks = [asyncio.create_task(call_one(idx, ref)) for idx, ref in enumerate(ref_configs)]
        ref_results: list[tuple[str, str]] = []
        try:
            for completed in asyncio.as_completed(tasks):
                _idx, label, text = await await_or_cancel(completed, controller.cancel_event)
                ref_results.append((label, text))
        except RunCancelled:
            await _cancel_and_reap(tasks)
            cancel_unsettled_reference_tasks()
            return ""
        except BaseException as exc:
            await _cancel_and_reap(tasks)
            cancel_unsettled_reference_tasks()
            if isinstance(exc, _RunEventDeliveryError):
                raise exc.cause
            raise

        # ── Phase 2: Aggregator synthesizes guidance ──
        await emitter.emit(
            EventType.HOST_AGGREGATION, ChannelId.HOST_MODELS, host,
            {"summary": f"🔄 正在综合 {len(ref_results)} 份参考意见…",
             "time_ms": int((time.time() - start_time) * 1000)},
        )
        try:
            guidance = await await_or_cancel(call_aggregator(
            host_config, messages, system_prompt, ref_results,
            temperature=float(host_config.get("temperature", 0.4)), timeout=60.0,
            owner="aggregator",
            ), controller.cancel_event)
        except RunCancelled:
            return ""

        guidance_artifact = persist_artifact(
            session_id=session.db_id, run_id=emitter.run_id,
            kind="moa-guidance", title="MOA 聚合指导", content=guidance,
            summary=bounded_summary(guidance, 500),
        )
        persist_working_memory(
            session_id=session.db_id, run_id=emitter.run_id,
            category="aggregator-guidance", content=bounded_summary(guidance, 1_500),
            source_ids=[guidance_artifact["artifact_id"]] if guidance_artifact else [],
        )
        if (payload := artifact_event_payload(guidance_artifact)) is not None:
            await emitter.emit(EventType.ARTIFACT, ChannelId.HOST_MODELS, host, payload)

        await emitter.emit(
            EventType.HOST_AGGREGATION, ChannelId.HOST_MODELS, host,
            {"summary": f"✅ 已综合参考意见，将传给主持人执行…",
             "time_ms": int((time.time() - start_time) * 1000)},
        )
      else:
        await emitter.emit(
            EventType.HOST_AGGREGATION, ChannelId.HOST_MODELS, host,
            {"summary": "⚡ 无参考模型，主持人直接使用系统能力…", "time_ms": 0},
        )

    except BudgetExceeded as exc:
        await emitter.emit(
            EventType.RUN_ERROR, ChannelId.USER_HOST, Actor.system(),
            {
                "code": "budget_exceeded", "message": f"Run stopped: {exc.reason.value}",
                "retryable": True, "stop_reason": exc.reason.value,
                "budget": controller.budget.snapshot(),
            },
            status=EventStatus.FAILED,
        )
        if not controller.is_terminal:
            controller.transition(RunState.FAILED)
        return ""
    finally:
        reset_run_budget(budget_token)

    if owns_controller and not controller.is_terminal:
        controller.transition(RunState.COMPLETED)
        if session.active_controller is controller:
            session.active_controller = None

    return guidance
