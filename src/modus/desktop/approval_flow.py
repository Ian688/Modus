"""Run-bound human approval bridge for the desktop transport."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from modus.desktop.approvals import approval_broker
from modus.desktop.db import create_approval, resolve_approval_record
from modus.desktop.events import Actor, ChannelId, EventStatus, EventType, RunEventEmitter
from modus.runtime.state import RunState

logger = logging.getLogger(__name__)


async def wait_for_user_approval(
    websocket: Any,
    session: Any,
    emitter: RunEventEmitter,
    request: dict[str, Any],
    *,
    timeout: float = 600.0,
) -> str:
    """Emit one typed request and fail closed on timeout or cancellation."""
    approval_id = uuid.uuid4().hex
    approval_key = (emitter.run_id, approval_id)
    controller = session.active_controller
    if controller is not None and controller.run_id == emitter.run_id:
        controller.transition(RunState.WAITING_APPROVAL)
        future = controller.register_approval(approval_id)
    else:
        future = asyncio.get_running_loop().create_future()
    session.pending_approvals[approval_key] = future
    approval_broker.register(emitter.run_id, approval_id, future)
    if session.db_id:
        try:
            create_approval(
                approval_id=approval_id,
                run_id=emitter.run_id,
                tool_name=str(request.get("tool_name") or "工具"),
                input_hash=str(request.get("input_hash") or ""),
                input_data=request.get("input") or {},
            )
        except Exception:
            logger.exception("approval audit create failed")
    try:
        approval_event = await emitter.emit(
            EventType.APPROVAL_REQUEST,
            ChannelId.USER_HOST,
            Actor.system("审批"),
            {
                "approval_id": approval_id,
                "run_id": emitter.run_id,
                "tool_name": str(request.get("tool_name") or "工具"),
                "tool_call_id": str(request.get("tool_call_id") or ""),
                "description": str(request.get("description") or ""),
                "input": request.get("input") or {},
                "input_hash": str(request.get("input_hash") or ""),
                "approval_expires_at": int(request.get("approval_expires_at") or 0),
                "danger_level": str(request.get("danger_level") or "medium"),
                "data_disclosure": str(request.get("data_disclosure") or "none"),
            },
            status=EventStatus.STARTED,
        )
        while True:
            try:
                decision = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
                cancelled = (
                    (controller is not None and controller.cancel_event.is_set())
                    or (session._cancel is not None and session._cancel.is_set())
                )
                normalized = (
                    "allow"
                    if str(decision).lower() in {"approve", "allow"} and not cancelled
                    else "deny"
                )
                resolve_approval_record(
                    approval_id,
                    normalized,
                    "run_cancelled" if cancelled else "user_decision",
                )
                await emitter.emit(
                    EventType.APPROVAL_RESOLVED,
                    ChannelId.USER_HOST,
                    Actor.system("审批"),
                    {
                        "approval_id": approval_id,
                        "run_id": emitter.run_id,
                        "tool_name": str(request.get("tool_name") or "工具"),
                        "tool_call_id": str(request.get("tool_call_id") or ""),
                        "decision": normalized,
                        "resolution_reason": "run_cancelled" if cancelled else "user_decision",
                    },
                    status=EventStatus.CANCELLED if normalized == "deny" else EventStatus.COMPLETED,
                    parent_event_id=approval_event.event_id,
                )
                return normalized
            except asyncio.CancelledError:
                if session._cancel is not None and session._cancel.is_set():
                    resolve_approval_record(approval_id, "deny", "run_cancelled")
                    await emitter.emit(
                        EventType.APPROVAL_RESOLVED,
                        ChannelId.USER_HOST,
                        Actor.system("审批"),
                        {
                            "approval_id": approval_id,
                            "run_id": emitter.run_id,
                            "tool_name": str(request.get("tool_name") or "工具"),
                            "tool_call_id": str(request.get("tool_call_id") or ""),
                            "decision": "deny",
                            "resolution_reason": "run_cancelled",
                        },
                        status=EventStatus.CANCELLED,
                        parent_event_id=approval_event.event_id,
                    )
                    return "deny"
                logger.warning(
                    "approval waiter cancelled while run remains active; preserving request run=%s id=%s",
                    emitter.run_id,
                    approval_id,
                )
            except TimeoutError:
                resolve_approval_record(approval_id, "deny", "approval_timeout")
                await emitter.emit(
                    EventType.APPROVAL_RESOLVED,
                    ChannelId.USER_HOST,
                    Actor.system("审批"),
                    {
                        "approval_id": approval_id,
                        "run_id": emitter.run_id,
                        "tool_name": str(request.get("tool_name") or "工具"),
                        "tool_call_id": str(request.get("tool_call_id") or ""),
                        "decision": "deny",
                        "resolution_reason": "approval_timeout",
                    },
                    status=EventStatus.CANCELLED,
                    parent_event_id=approval_event.event_id,
                )
                return "deny"
    finally:
        session.pending_approvals.pop(approval_key, None)
        approval_broker.remove(emitter.run_id, approval_id)
        if controller is not None and controller.run_id == emitter.run_id:
            controller.remove_approval(approval_id)
            if controller.state is RunState.WAITING_APPROVAL and not controller.cancel_event.is_set():
                controller.transition(RunState.RUNNING)


def resolve_pending_approval(
    session: Any, run_id: str, approval_id: str, decision: str,
) -> bool:
    """Resolve exactly one run-bound approval, including after reconnect."""
    if not run_id or not approval_id:
        return False
    if approval_broker.resolve(run_id, approval_id, decision):
        return True
    future = session.pending_approvals.get((run_id, approval_id))
    if future is None or future.done():
        return False
    future.set_result(decision)
    return True
