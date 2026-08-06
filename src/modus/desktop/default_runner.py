from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from modus.desktop.db import add_message, settle_run_event, update_session
from modus.desktop.events import Actor, ChannelId, EventStatus, EventType, RunEventEmitter
from modus.desktop.memory import consolidate_run_memories
from modus.modes import DEFAULT_MODE
from modus.runtime.controller import RunController
from modus.runtime.state import RunState
from modus.runtime.budget import StopReason, bind_run_budget, reset_run_budget
from modus.tools.payload import artifact_ids_from_result
from modus.types import Message

logger = logging.getLogger(__name__)


async def _maybe_consolidate(session: Any, user_message: str) -> None:
    """Best-effort auto-memorization after a completed default run.

    Gated on ``config.memory.auto_memorize``; never raises so a model failure
    cannot disturb the run's already-terminal state.
    """
    engine_config = getattr(session, "engine", None)
    memory_config = getattr(getattr(engine_config, "config", None), "memory", None)
    if not memory_config or not bool(getattr(memory_config, "auto_memorize", False)):
        return
    llm_config = getattr(getattr(engine_config, "config", None), "llm", None)
    if llm_config is None or not getattr(llm_config, "api_key", ""):
        return
    # The terminal envelope carries only budget/turn counts; use the last
    # persisted assistant message as consolidation content.
    from modus.desktop.db import get_latest_assistant_message

    assistant_text = get_latest_assistant_message(session.db_id) or ""
    if not assistant_text:
        return
    try:
        await consolidate_run_memories(
            session_id=session.db_id,
            user_message=user_message,
            assistant_text=assistant_text,
            provider=str(llm_config.provider or "deepseek"),
            model=str(llm_config.model or "deepseek-v4-flash"),
            api_key=str(llm_config.api_key or ""),
            base_url=llm_config.base_url,
        )
    except Exception:
        logger.debug("auto-memorize consolidation failed", exc_info=True)


def _settle_terminal_fallback(
    session: Any, event: Any,
) -> bool:
    """Persist an emitted terminal envelope when its normal audit was absent.

    The same immutable event is reused so Run, root task, and transcript remain
    one atomic fact.  If the normal audit already won, ``settle_run_event`` is a
    total no-op and the first terminal remains authoritative.
    """
    if not session.db_id or event is None:
        return False
    try:
        return settle_run_event(session.db_id, event.to_wire())
    except Exception:
        logger.exception(
            "could not persist terminal fallback for run=%s",
            getattr(event, "run_id", ""),
        )
        return False


async def _finalize_transport_disconnect(
    session: Any,
    emitter: RunEventEmitter,
    controller: RunController,
) -> None:
    """Park a disconnected default run when enabled, else cancel it.

    ``RunEventEmitter`` audits before writing to the WebSocket, so attempting the
    typed terminal event preserves a replayable explanation when the transport
    fails during its send.  The SQLite writes below are deliberately idempotent
    fallback: a disconnect must never strand the run or its root task in the
    running state merely because the audit hook was absent or failed.
    """
    engine_config = getattr(getattr(session, "engine", None), "config", None)
    features = getattr(engine_config, "features", None)
    park = bool(getattr(features, "park_on_disconnect", False))
    if park:
        session.park_run(emitter, controller)
        return
    session.cancel_stream()
    controller.budget.finish(StopReason.CANCELLED)
    payload = {
        "code": "transport_disconnected",
        "message": "连接已断开，运行已取消。",
        "retryable": True,
        "stop_reason": "cancelled",
        "disconnect_reason": "transport_disconnected",
        "budget": controller.budget.snapshot(),
        "verification": controller.budget.verification.snapshot(),
    }
    terminal_event = None
    try:
        terminal_event = await emitter.emit(
            EventType.RUN_ERROR, ChannelId.USER_HOST, Actor.system(), payload,
            status=EventStatus.CANCELLED,
        )
    except Exception:
        # The peer is gone.  ``emit`` audits before sending, but its audit sink
        # is best effort, so persistence is finalized below either way.
        logger.debug("disconnect terminal event could not be delivered", exc_info=True)
        terminal_event = emitter.terminal_event

    _settle_terminal_fallback(session, terminal_event)


async def _finalize_provider_stream_ended(
    websocket: WebSocket,
    session: Any,
    emitter: RunEventEmitter,
    controller: RunController,
    message: str,
) -> None:
    """Fail a provider stream that ended without an explicit terminal event.

    Provider adapters must finish with ``done`` or ``error``.  A natural async
    iterator EOF before either signal is not success: without this guard the
    in-memory owner was released while the durable Run and root task remained
    ``running`` until the next process restart.
    """
    controller.budget.finish(StopReason.ENGINE_ERROR)
    error_message = "模型响应流在返回终态前结束。"
    payload = {
        "code": "provider_stream_ended",
        "message": error_message,
        "retryable": True,
        "stop_reason": "engine_error",
        "budget": controller.budget.snapshot(),
        "verification": controller.budget.verification.snapshot(),
    }

    # A provider EOF has no returned history snapshot.  Preserve the submitted
    # user turn just as the explicit provider-error path does.
    terminal_event = None
    try:
        if session.db_id:
            add_message(session.db_id, "user", message, token_count=0)
        session.main_history.append(Message(role="user", content=message))
    except Exception:
        logger.exception("could not preserve user message after provider stream EOF")

    try:
        # The audit sink runs before transport delivery, so the typed failure is
        # normally replayable even if the socket disappears during this send.
        terminal_event = await emitter.emit(
            EventType.RUN_ERROR, ChannelId.USER_HOST, Actor.system(), payload,
            status=EventStatus.FAILED,
        )
    except Exception:
        logger.debug("provider EOF terminal event could not be delivered", exc_info=True)
        terminal_event = emitter.terminal_event

    _settle_terminal_fallback(session, terminal_event)

    try:
        await websocket.send_json({
            "type": "done",
            "total_tokens": controller.budget.total_tokens,
            "total_turns": controller.budget.turns,
            "stop_reason": "engine_error",
            "budget": controller.budget.snapshot(),
            "verification": controller.budget.verification.snapshot(),
        })
    except Exception:
        logger.debug("provider EOF control terminal could not be delivered", exc_info=True)


async def stream_to_ws(
    websocket: WebSocket,
    session: Any,
    message: str,
    *,
    mode: str = DEFAULT_MODE,
    emitter: RunEventEmitter | None = None,
    controller: RunController | None = None,
    user_visible_message: str | None = None,
    emit_user_message: bool = True,
    transient_context: list[Message] | None = None,
    verification_required: bool = False,
    audit_event_for: Any,
    wait_for_user_approval_callback: Any,
    extract_worldview: Any,
    compress_history: Any,
    persist_run_start: Any = None,
    manage_controller: bool | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> RunEventEmitter:
    """Run the host Agent and emit its visible transcript as typed events.

    ``agent_event`` is the only transcript contract.  Plain packets emitted by
    this runner are control-plane notifications (for example ``done`` to
    release the composer) and must never own rendered conversation content.  A
    caller may provide an emitter to keep an MOA/Peri run under one run_id; the
    default Agent creates its own.
    """
    emitter = emitter or RunEventEmitter(
        run_id=f"run_{uuid.uuid4().hex}", mode=mode, send_json=websocket.send_json,
        audit_event=audit_event_for(session),
    )
    owns_controller = controller is None if manage_controller is None else manage_controller
    if controller is None:
        engine_config = getattr(getattr(session, "engine", None), "config", None)
        controller = RunController.from_config(
            run_id=emitter.run_id, mode=mode, config=engine_config,
        ) if engine_config is not None else RunController(run_id=emitter.run_id, mode=mode)
    if controller.state is RunState.CREATED:
        controller.transition(RunState.RUNNING)
    session.active_controller = controller
    if persist_run_start is not None:
        persisted = persist_run_start(
            session, emitter, controller, mode,
            verification_required=verification_required,
        )
        if (
            session.db_id
            and persisted is not None
            and (
                str((persisted or {}).get("run_id") or "") != emitter.run_id
                or not emitter.root_task_id
            )
        ):
            # Admission is a precondition for provider execution in every
            # entry path, including verification retry and compatibility
            # clients that do not yet send an explicit request_id.
            if not controller.is_terminal:
                controller.transition(RunState.FAILED)
            if session.active_controller is controller:
                session.active_controller = None
            raise RuntimeError("Run admission could not persist its root task")
    # Keep the legacy token synchronized while callers migrate to the controller.
    legacy_cancel_event = session._ensure_cancel()
    legacy_cancel_event.clear()
    cancel_event = controller.cancel_event
    host = Actor.host("primary", "主持人")
    if owns_controller:
        await emitter.emit(
            EventType.RUN_STARTED, ChannelId.USER_HOST, Actor.system(),
            {"state": "running", "mode": mode, "budget": controller.budget.snapshot()},
            status=EventStatus.STARTED,
        )
    if emit_user_message:
        payload: dict[str, Any] = {
            "markdown": user_visible_message if user_visible_message is not None else message,
        }
        if attachments:
            payload["attachments"] = attachments
        await emitter.emit(
            EventType.USER_MESSAGE, ChannelId.USER_HOST, Actor.user(), payload,
        )
    text_event_id: str | None = None
    thinking_event_id: str | None = None
    tool_event_ids: dict[str, str] = {}
    completed_normally = False
    provider_terminal_received = False
    budget_token = bind_run_budget(controller.budget)
    if verification_required:
        controller.budget.verification.require_verification()
    from modus.agent.context import SessionContextProvider

    context_provider = SessionContextProvider()
    memory_message = None
    effective_history = context_provider.effective_history(session, transient=transient_context)
    memory_context = context_provider.memory_text(session.db_id)
    if memory_context:
        memory_message = Message(role="system", content=memory_context)
    history_length_before_run = len(session.main_history) + len(transient_context or []) + (1 if memory_message else 0)
    try:
        if session.engine is None:
            raise RuntimeError("Session engine is not configured")
        async def approval_callback(request: dict[str, Any]) -> str:
            return await wait_for_user_approval_callback(websocket, session, emitter, request)

        async for event in session.engine.ask(
            message, history=effective_history, approval_callback=approval_callback,
            cancel_event=cancel_event, budget=controller.budget,
            session_id=session.db_id or None, run_id=emitter.run_id,
        ):
            if cancel_event.is_set():
                if session.db_id:
                    add_message(session.db_id, "user", message, token_count=0)
                session.main_history.append(Message(role="user", content=message))
                controller.budget.finish(StopReason.CANCELLED)
                await emitter.emit(
                    EventType.RUN_ERROR, ChannelId.USER_HOST, Actor.system(),
                    {"code": "cancelled", "message": "用户中断", "retryable": False,
                     "stop_reason": "cancelled", "budget": controller.budget.snapshot(),
                     "verification": controller.budget.verification.snapshot()},
                    status=EventStatus.CANCELLED,
                )
                await websocket.send_json({
                    "type": "done", "total_tokens": controller.budget.total_tokens,
                    "total_turns": controller.budget.turns, "stop_reason": "cancelled",
                    "budget": controller.budget.snapshot(),
                    "verification": controller.budget.verification.snapshot(),
                })
                return emitter
            event_type = event.get("type", "")
            if event_type == "text_delta":
                text = event.get("text", "")
                typed = await emitter.emit(
                    EventType.HOST_RESPONSE, ChannelId.USER_HOST, host,
                    {"markdown": text, "streaming": True},
                    status=EventStatus.STREAMING,
                    event_id=text_event_id,
                )
                text_event_id = text_event_id or typed.event_id
            elif event_type == "thinking_delta":
                thinking = event.get("thinking", event.get("text", ""))
                typed = await emitter.emit(
                    EventType.HOST_THINKING, ChannelId.USER_HOST, host,
                    {"text": thinking, "streaming": True},
                    status=EventStatus.STREAMING,
                    event_id=thinking_event_id,
                )
                thinking_event_id = thinking_event_id or typed.event_id
            elif event_type == "tool_call":
                name = event.get("name", "")
                input_data = event.get("input", {})
                tool_call_id = str(event.get("tool_call_id") or "")
                tool_event = await emitter.emit(
                    EventType.TOOL_CALL, ChannelId.HOST_MODELS, Actor.tool(name),
                    {"tool_call_id": tool_call_id, "name": name, "input": input_data},
                    parent_event_id=text_event_id,
                )
                if tool_call_id:
                    tool_event_ids[tool_call_id] = tool_event.event_id
            elif event_type == "tool_result":
                name = event.get("name", "")
                result = event.get("result", "")
                is_error = bool(event.get("is_error", False))
                tool_call_id = str(event.get("tool_call_id") or "")
                result_payload = {
                    "tool_call_id": tool_call_id, "name": name, "result": result,
                    "is_error": is_error,
                }
                if event.get("display_summary"):
                    result_payload["display_summary"] = event["display_summary"]
                if event.get("metadata"):
                    result_payload["metadata"] = event["metadata"]
                if event.get("disclosure"):
                    result_payload["disclosure"] = event["disclosure"]
                artifact_ids = artifact_ids_from_result(event)
                await emitter.emit(
                    EventType.TOOL_RESULT, ChannelId.HOST_MODELS, Actor.tool(name), result_payload,
                    status=EventStatus.FAILED if is_error else EventStatus.COMPLETED,
                    parent_event_id=tool_event_ids.get(tool_call_id),
                    artifact_ids=artifact_ids,
                )
            elif event_type == "usage":
                # Usage is accounted for by the run budget and included in the
                # terminal event/control packet.  It has no separate rendering
                # or transport ownership.
                continue
            elif event_type == "done":
                provider_terminal_received = True
                stop_reason = str(event.get("stop_reason") or "completed")
                reason_map = {
                    "completed": StopReason.COMPLETED,
                    "max_turns": StopReason.MAX_TURNS,
                    "token_limit": StopReason.TOKEN_LIMIT,
                    "wall_time": StopReason.WALL_TIME,
                    "cancelled": StopReason.CANCELLED,
                    "engine_error": StopReason.ENGINE_ERROR,
                    "failed": StopReason.FAILED,
                    "verification_required": StopReason.VERIFICATION_REQUIRED,
                    "verification_retry_limit": StopReason.VERIFICATION_RETRY_LIMIT,
                }
                controller.budget.finish(reason_map.get(stop_reason, StopReason.FAILED))
                returned_history = list(event.get("messages") or [])
                # ``messages`` is a full model-context snapshot.
                # A defensive fallback keeps a user turn visible even when a
                # lightweight provider returns no context snapshot.
                new_history = returned_history[history_length_before_run:]
                if not new_history:
                    returned_history = [*session.main_history, Message(role="user", content=message)]
                    new_history = [returned_history[-1]]
                # ``messages`` is a full model-context snapshot. SQLite is an
                # append log, so persist only messages created by this turn.
                for history_message in new_history:
                    content = history_message.content if isinstance(history_message.content, str) else ""
                    if not session.db_id:
                        continue
                    add_message(
                        session.db_id, history_message.role, content,
                        tool_calls=history_message.tool_calls, token_count=0,
                        tool_call_id=history_message.tool_call_id,
                    )
                session.main_history = [
                    item for item in returned_history
                    if item is not memory_message
                    and item not in (transient_context or [])
                    and not (
                        item.role == "system" and isinstance(item.content, str)
                        and item.content.startswith("[SESSION MEMORY — REFERENCE ONLY]")
                    )
                ]
                if session.db_id and not session.worldview:
                    session.worldview = extract_worldview(message, event)
                    session.world_view_history.append(session.worldview)
                    update_session(session.db_id, worldview=session.worldview, world_view_history=json.dumps(session.world_view_history, ensure_ascii=False))
                    await websocket.send_json({"type": "worldview_updated", "worldview": session.worldview})
                compaction = compress_history(session, run_id=emitter.run_id)
                if compaction:
                    await emitter.emit(
                        EventType.CONTEXT_COMPACTED, ChannelId.USER_HOST,
                        Actor.system(label="上下文"), compaction,
                    )
                terminal_payload = {
                    "total_tokens": event.get("total_tokens", 0),
                    "total_turns": event.get("total_turns", 0),
                    "stop_reason": stop_reason,
                    "budget": event.get("budget") or controller.budget.snapshot(),
                    "verification": event.get("verification") or controller.budget.verification.snapshot(),
                }
                if stop_reason == "completed":
                    await emitter.emit(
                        EventType.RUN_COMPLETED, ChannelId.USER_HOST, host, terminal_payload,
                    )
                    completed_normally = True
                    if session.db_id and getattr(
                        getattr(getattr(session, "engine", None), "config", None), "memory", None
                    ) is not None:
                        await _maybe_consolidate(session, message)
                elif stop_reason in {"max_turns", "token_limit", "wall_time"}:
                    await emitter.emit(
                        EventType.RUN_ERROR, ChannelId.USER_HOST, Actor.system(),
                        {**terminal_payload, "code": "budget_exceeded", "message": f"Run stopped: {stop_reason}", "retryable": True},
                        status=EventStatus.FAILED,
                    )
                else:
                    stop_message = (
                        "代码已修改，但尚未获得通过的验证证据。"
                        if stop_reason == "verification_required"
                        else "验证连续失败，已达到自动修复重试上限。"
                        if stop_reason == "verification_retry_limit"
                        else f"Run stopped: {stop_reason}"
                    )
                    await emitter.emit(
                        EventType.RUN_ERROR, ChannelId.USER_HOST, Actor.system(),
                        {
                            **terminal_payload, "code": stop_reason,
                            "message": stop_message,
                            "retryable": stop_reason in {"verification_required", "verification_retry_limit"},
                        },
                        status=EventStatus.CANCELLED if stop_reason == "cancelled" else EventStatus.FAILED,
                    )
                await websocket.send_json({
                    "type": "done", "total_tokens": event.get("total_tokens", 0),
                    "total_turns": event.get("total_turns", 0),
                    "stop_reason": stop_reason, "budget": terminal_payload["budget"],
                    "verification": terminal_payload["verification"],
                })
                break
            elif event_type == "error":
                provider_terminal_received = True
                detail = str(event.get("error", ""))
                error_reason = str(
                    event.get("stop_reason")
                    or ("cancelled" if cancel_event.is_set() else "engine_error")
                )
                error_map = {
                    "cancelled": StopReason.CANCELLED,
                    "engine_error": StopReason.ENGINE_ERROR,
                    "failed": StopReason.FAILED,
                }
                controller.budget.finish(error_map.get(error_reason, StopReason.ENGINE_ERROR))
                # Persist the user message even on error so it is not lost on re-open.
                if session.db_id:
                    add_message(session.db_id, "user", message, token_count=0)
                session.main_history.append(Message(role="user", content=message))
                await emitter.emit(
                    EventType.RUN_ERROR, ChannelId.USER_HOST, Actor.system(),
                    {
                        "code": error_reason,
                        "message": detail, "retryable": False,
                        "stop_reason": error_reason,
                        "budget": event.get("budget") or controller.budget.snapshot(),
                        "verification": event.get("verification") or controller.budget.verification.snapshot(),
                    },
                    status=EventStatus.CANCELLED if error_reason == "cancelled" else EventStatus.FAILED,
                )
                await websocket.send_json({
                    "type": "done", "total_tokens": controller.budget.total_tokens,
                    "total_turns": controller.budget.turns,
                    "stop_reason": error_reason,
                    "budget": event.get("budget") or controller.budget.snapshot(),
                    "verification": event.get("verification") or controller.budget.verification.snapshot(),
                })
                break
        if not provider_terminal_received:
            await _finalize_provider_stream_ended(
                websocket, session, emitter, controller, message,
            )
    except WebSocketDisconnect:
        # The request/response run is not reconnectable yet.  Do not continue
        # execution without a transport capable of delivering user approval.
        await _finalize_transport_disconnect(session, emitter, controller)
        logger.info("agent stream transport disconnected; run cancelled")
        return emitter
    except Exception as exc:
        logger.exception("stream error")
        controller.budget.finish(StopReason.FAILED)
        # Persist user message even on stream crash so it survives reopen
        try:
            if session.db_id:
                add_message(session.db_id, "user", message, token_count=0)
            session.main_history.append(Message(role="user", content=message))
        except Exception:
            pass
        try:
            await emitter.emit(
                EventType.RUN_ERROR, ChannelId.USER_HOST, Actor.system(),
                {"code": "stream_error", "message": str(exc), "retryable": False,
                 "stop_reason": "failed", "budget": controller.budget.snapshot()},
                status=EventStatus.FAILED,
            )
            await websocket.send_json({
                "type": "done", "total_tokens": controller.budget.total_tokens,
                "total_turns": controller.budget.turns, "stop_reason": "failed",
                "budget": controller.budget.snapshot(),
            })
        except Exception:
            pass
    finally:
        reset_run_budget(budget_token)
        if owns_controller:
            if controller.cancel_event.is_set():
                controller.cancel_complete()
            elif completed_normally:
                controller.transition(RunState.COMPLETED)
            elif not controller.is_terminal:
                controller.transition(RunState.FAILED)
            if session.active_controller is controller:
                session.active_controller = None
    return emitter
