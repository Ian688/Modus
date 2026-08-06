"""Typed WebSocket event contract for Modus Agent runs.

The contract deliberately has no FastAPI, database, or UI dependency so all
modes can share it and tests can validate wire messages in isolation.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from modus.modes import normalize_mode
from modus.desktop.workbench import EVENT_SCHEMA


class ActorKind(StrEnum):
    USER = "user"
    HOST = "host"
    REFERENCE_MODEL = "reference_model"
    SUBAGENT = "subagent"
    TOOL = "tool"
    SYSTEM = "system"


class ChannelId(StrEnum):
    USER_HOST = "user_host"
    HOST_MODELS = "host_models"


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    USER_MESSAGE = "user_message"
    HOST_THINKING = "host_thinking"
    HOST_RESPONSE = "host_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ARTIFACT = "artifact"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_RESOLVED = "approval_resolved"
    HOST_DISPATCH = "host_dispatch"
    REFERENCE_STARTED = "reference_started"
    REFERENCE_RESPONSE = "reference_response"
    SUBTASK_ASSIGNMENT = "subtask_assignment"
    SUBAGENT_PROGRESS = "subagent_progress"
    SUBAGENT_TOOL_CALL = "subagent_tool_call"
    SUBAGENT_TOOL_RESULT = "subagent_tool_result"
    SUBAGENT_RESPONSE = "subagent_response"
    HOST_REVIEW = "host_review"
    HOST_AGGREGATION = "host_aggregation"
    CONTEXT_COMPACTED = "context_compacted"
    RUN_ERROR = "run_error"
    RUN_COMPLETED = "run_completed"


class EventStatus(StrEnum):
    STARTED = "started"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Actor:
    kind: ActorKind
    id: str
    label: str

    @classmethod
    def user(cls, label: str = "用户", actor_id: str = "user") -> "Actor":
        return cls(ActorKind.USER, actor_id, label)

    @classmethod
    def host(cls, actor_id: str, label: str = "主持人") -> "Actor":
        return cls(ActorKind.HOST, actor_id, label)

    @classmethod
    def reference_model(cls, actor_id: str, label: str) -> "Actor":
        return cls(ActorKind.REFERENCE_MODEL, actor_id, label)

    @classmethod
    def subagent(cls, actor_id: str, label: str) -> "Actor":
        return cls(ActorKind.SUBAGENT, actor_id, label)

    @classmethod
    def tool(cls, actor_id: str, label: str | None = None) -> "Actor":
        return cls(ActorKind.TOOL, actor_id, label or actor_id)

    @classmethod
    def system(cls, label: str = "系统", actor_id: str = "system") -> "Actor":
        return cls(ActorKind.SYSTEM, actor_id, label)

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "Actor":
        return cls(
            kind=ActorKind(value["kind"]),
            id=str(value["id"]),
            label=str(value["label"]),
        )

    def to_wire(self) -> dict[str, str]:
        return {"kind": self.kind.value, "id": self.id, "label": self.label}


@dataclass(frozen=True, slots=True)
class AgentEvent:
    event_id: str
    run_id: str
    channel_id: ChannelId
    parent_event_id: str | None
    sequence: int
    timestamp: str
    mode: str
    actor: Actor
    type: EventType
    status: EventStatus
    payload: dict[str, Any]
    revision: int = 0
    part_id: str = ""
    workspace_id: str = ""
    task_id: str | None = None
    artifact_ids: tuple[str, ...] = ()
    schema: str = EVENT_SCHEMA

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        sequence: int,
        mode: str,
        channel_id: ChannelId,
        actor: Actor,
        event_type: EventType,
        payload: dict[str, Any],
        status: EventStatus = EventStatus.COMPLETED,
        parent_event_id: str | None = None,
        event_id: str | None = None,
        part_id: str | None = None,
        workspace_id: str = "",
        task_id: str | None = None,
        artifact_ids: list[str] | tuple[str, ...] | None = None,
        revision: int = 0,
    ) -> "AgentEvent":
        if not run_id:
            raise ValueError("run_id is required")
        if sequence < 1:
            raise ValueError("sequence must start at 1")
        if not mode:
            raise ValueError("mode is required")
        if payload is None or not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        if revision < 0:
            raise ValueError("revision must be non-negative")
        return cls(
            event_id=event_id or f"evt_{uuid4().hex}",
            run_id=run_id,
            channel_id=ChannelId(channel_id),
            parent_event_id=parent_event_id,
            sequence=sequence,
            timestamp=datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            mode=normalize_mode(mode),
            actor=actor,
            type=EventType(event_type),
            status=EventStatus(status),
            payload=payload,
            revision=revision,
            part_id=part_id or f"part_{uuid4().hex}",
            workspace_id=str(workspace_id or ""),
            task_id=str(task_id) if task_id else None,
            artifact_ids=tuple(str(item) for item in (artifact_ids or []) if item),
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "channel_id": self.channel_id.value,
            "parent_event_id": self.parent_event_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "mode": self.mode,
            "actor": self.actor.to_wire(),
            "type": self.type.value,
            "status": self.status.value,
            "payload": self.payload,
            "revision": self.revision,
            "part_id": self.part_id,
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "artifact_ids": list(self.artifact_ids),
            "schema": self.schema,
        }


SendJson = Callable[[dict[str, Any]], Awaitable[None]]
AuditEvent = Callable[[dict[str, Any]], Awaitable[bool | None] | bool | None]


class RunEventEmitter:
    """Allocates ordered immutable events for exactly one user task run."""

    def __init__(
        self, *, run_id: str, mode: str, send_json: SendJson,
        audit_event: AuditEvent | None = None, workspace_id: str = "",
        root_task_id: str | None = None,
    ) -> None:
        if not run_id:
            raise ValueError("run_id is required")
        self.run_id = run_id
        self.mode = normalize_mode(mode)
        self._send_json = send_json
        self._audit_event = audit_event
        self._sequence = 0
        self._events: dict[str, AgentEvent] = {}
        self._terminal_event_id: str | None = None
        self.workspace_id = str(workspace_id or "")
        self.root_task_id = str(root_task_id) if root_task_id else None

    def bind_context(self, *, workspace_id: str, root_task_id: str | None) -> None:
        """Bind authoritative ledger identities before the first event."""
        self.workspace_id = str(workspace_id or "")
        self.root_task_id = str(root_task_id) if root_task_id else None

    def rebind(self, send_json: SendJson) -> None:
        """Re-point the transport of a parked run, e.g. to a new socket.

        ``emit`` audits before sending, so swapping the transport never loses
        an event: the durable SQLite ledger is written either way, and only the
        live delivery target changes.
        """
        self._send_json = send_json

    @property
    def terminal_event(self) -> AgentEvent | None:
        """Return the accepted terminal envelope even if transport delivery failed."""
        if self._terminal_event_id is None:
            return None
        return self._events.get(self._terminal_event_id)

    async def emit(
        self,
        event_type: EventType,
        channel_id: ChannelId,
        actor: Actor,
        payload: dict[str, Any],
        *,
        status: EventStatus = EventStatus.COMPLETED,
        parent_event_id: str | None = None,
        event_id: str | None = None,
        part_id: str | None = None,
        task_id: str | None = None,
        artifact_ids: list[str] | tuple[str, ...] | None = None,
    ) -> AgentEvent:
        terminal = event_type in {EventType.RUN_COMPLETED, EventType.RUN_ERROR}
        if terminal and self._terminal_event_id is not None:
            return self._events[self._terminal_event_id]
        resolved_task_id = str(task_id or payload.get("task_id") or self.root_task_id or "") or None
        resolved_artifacts = tuple(
            str(item) for item in (
                artifact_ids
                or payload.get("artifact_ids")
                or ([payload.get("artifact_id")] if payload.get("artifact_id") else [])
            ) if item
        )
        # A streaming ContentBlock keeps one stable event_id.  Later deltas
        # replace its payload while retaining its original sequence/position.
        if event_id and event_id in self._events:
            prior = self._events[event_id]
            merged_payload = dict(prior.payload)
            for key, value in payload.items():
                if key in {"markdown", "text"} and isinstance(value, str):
                    merged_payload[key] = str(merged_payload.get(key, "")) + value
                else:
                    merged_payload[key] = value
            event = AgentEvent(
                event_id=prior.event_id,
                run_id=prior.run_id,
                channel_id=prior.channel_id,
                parent_event_id=prior.parent_event_id,
                sequence=prior.sequence,
                timestamp=prior.timestamp,
                mode=prior.mode,
                actor=prior.actor,
                type=prior.type,
                status=status,
                payload=merged_payload,
                revision=prior.revision + 1,
                part_id=prior.part_id,
                workspace_id=prior.workspace_id,
                task_id=prior.task_id,
                artifact_ids=prior.artifact_ids,
                schema=prior.schema,
            )
        else:
            self._sequence += 1
            event = AgentEvent.create(
                run_id=self.run_id,
                sequence=self._sequence,
                mode=self.mode,
                channel_id=channel_id,
                actor=actor,
                event_type=event_type,
                status=status,
                parent_event_id=parent_event_id,
                event_id=event_id,
                payload=payload,
                part_id=part_id,
                workspace_id=self.workspace_id,
                task_id=resolved_task_id,
                artifact_ids=resolved_artifacts,
            )
        self._events[event.event_id] = event
        wire_event = event.to_wire()
        if self._audit_event is not None:
            audited = self._audit_event(wire_event)
            if inspect.isawaitable(audited):
                audited = await audited
            if terminal and audited is False:
                prior = self._events.pop(event.event_id, None)
                if prior is not None and prior.sequence == self._sequence:
                    self._sequence -= 1
                if self._terminal_event_id is not None:
                    return self._events[self._terminal_event_id]
                raise RuntimeError("terminal run settlement was rejected")
        if terminal:
            # Durable audit is the authority.  Lock the in-process emitter only
            # after it succeeds, then never transmit a contradictory terminal.
            self._terminal_event_id = event.event_id
        await self._send_json({"type": "agent_event", "event": wire_event})
        return event
