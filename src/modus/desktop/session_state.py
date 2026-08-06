"""In-memory desktop session ownership and background-run admission."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from modus.desktop.approvals import approval_broker
from modus.desktop.db import create_session, get_session
from modus.desktop.workspace import WorkspaceIdentity
from modus.modes import DEFAULT_MODE, normalize_mode
from modus.runtime.controller import RunController
from modus.types import Message

logger = logging.getLogger(__name__)


_active_persisted_runs: dict[str, tuple["DaoSession", asyncio.Task[Any]]] = {}

# A completed provider task can become visible a few milliseconds before all
# of its SQLite settlement writes are readable.  Keep the first release check
# fail-closed, then tolerate only that short visibility window.  A genuinely
# inconsistent ledger is deliberately *not* polled forever.
_SETTLEMENT_RECHECK_DELAYS_SECONDS: tuple[float, ...] = (0.05, 0.20, 0.75)


@dataclass
class DaoSession:
    """Mutable state for one connected desktop conversation."""

    id: str
    db_id: str = ""
    workspace_id: str = ""
    workspace_root: str = ""
    workspace_name: str = ""
    owner_id: str = ""
    worldview: str = ""
    world_view_history: list[str] = field(default_factory=list)
    system_prompt: str = ""
    model_id: str = ""
    mode: str = DEFAULT_MODE
    mode_config: dict[str, Any] = field(default_factory=dict)
    reasoning_effort: str | None = None
    main_history: list[Message] = field(default_factory=list)
    model_discovery: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    engine: Any = None
    _cancel: asyncio.Event | None = None
    pending_approvals: dict[tuple[str, str], asyncio.Future[str]] = field(default_factory=dict)
    active_run_task: asyncio.Task[Any] | None = None
    active_run_session_id: str | None = None
    active_run_id: str | None = None
    active_controller: RunController | None = None
    parked: bool = False
    parked_emitter: Any = None
    parked_controller: RunController | None = None
    settlement_recheck_task: asyncio.Task[None] | None = field(
        default=None, repr=False,
    )
    pending_session_create_key: str | None = None
    extensions_revision: int = 0

    def cancel_stream(self) -> None:
        """Cancel the active run and deny every pending side-effect request."""
        if self.active_controller is not None:
            self.active_controller.cancel()
        if self._cancel:
            self._cancel.set()
        for run_id, _approval_id in tuple(self.pending_approvals):
            approval_broker.deny_run(run_id)
        for future in tuple(self.pending_approvals.values()):
            if not future.done():
                future.set_result("deny")
        self.pending_approvals.clear()

    def park_run(self, emitter: Any, controller: RunController | None) -> None:
        """Detach transport but keep the run executing; never cancels work.

        Used on a WebSocket disconnect when ``park_on_disconnect`` is enabled:
        the provider stream keeps running, every emitted event is still audited
        to SQLite, and a later ``resume_parked`` re-points the emitter at the
        reconnected socket.  Pending approvals are preserved (denied only by an
        explicit cancel, not by a mere disconnect).
        """
        self.parked = True
        self.parked_emitter = emitter
        self.parked_controller = controller
        if controller is not None:
            controller.pause()
        if emitter is not None:
            emitter.rebind(_noop_send_json)

    def resume_parked(self, send_json: Any) -> bool:
        """Rebind a parked run to a live socket; returns False when not parked."""
        if not self.parked or self.parked_emitter is None:
            return False
        self.parked_emitter.rebind(send_json)
        if self.parked_controller is not None:
            self.parked_controller.resume()
        self.parked = False
        return True

    def _ensure_cancel(self) -> asyncio.Event:
        if self._cancel is None:
            self._cancel = asyncio.Event()
        self._cancel.clear()
        return self._cancel

    def handle_disconnect(self, emitter: Any, controller: RunController | None) -> bool:
        """Park the run on disconnect when enabled, else cancel fail-closed.

        Returns True when the run was parked (kept executing), False when it was
        cancelled (old behaviour).  Parking keeps the emitter audited to SQLite
        and the controller paused for a later ``resume_parked``.
        """
        engine_config = getattr(getattr(self.engine, "config", None), "features", None)
        park = bool(getattr(engine_config, "park_on_disconnect", False))
        if park and (emitter is not None or self.active_controller is not None):
            self.park_run(emitter, controller)
            return True
        self.cancel_stream()
        return False


async def _noop_send_json(_message: dict) -> None:
    """Transport sink for a parked run: drop live delivery, keep the audit."""
    return None


class SessionManager:
    def __init__(self) -> None:
        # Revisions are monotonic only within one server process.  The browser
        # uses this opaque epoch to distinguish a genuine stale packet from a
        # low revision produced after a Desktop restart.
        self.server_epoch = uuid.uuid4().hex
        self._sessions: dict[str, DaoSession] = {}
        self._websockets: dict[str, Any] = {}
        self._model_repository_revision = 0
        self._session_catalog_revision = 0
        self._skills_revision = 0
        self._extensions_revision = 0
        # Explicit run submissions are admitted under one short process-wide
        # lock.  SQLite remains the durable uniqueness guard; this lock also
        # prevents two transient reconnects from creating separate blank
        # sessions before either can reserve the shared client request ID.
        self.run_admission_lock = asyncio.Lock()
        # Idempotency is process-wide because a reconnect creates a new
        # in-memory DaoSession. Keys are random client intents and are never
        # exposed to the browser after acknowledgement.
        self._persisted_create_requests: dict[str, str] = {}

    def create(
        self, engine: Any = None, *, workspace_root: str | None = None,
    ) -> DaoSession:
        from modus.desktop import accounts

        root = str(workspace_root or getattr(engine, "cwd", None) or "").strip()
        workspace = WorkspaceIdentity.from_path(root) if root else None

        session = DaoSession(
            id=uuid.uuid4().hex[:12],
            engine=engine,
            extensions_revision=self._extensions_revision,
            workspace_id=workspace.workspace_id if workspace else "",
            workspace_root=workspace.root if workspace else "",
            workspace_name=workspace.name if workspace else "",
            owner_id=str(accounts.ensure_default_user()["user_id"]),
        )
        self._sessions[session.id] = session
        return session

    def persist_first(self, session: DaoSession) -> dict[str, Any] | None:
        """Persist a transient runtime session exactly once.

        ``DaoSession.id`` identifies the live WebSocket owner and never changes.
        ``db_id`` is empty until SQLite has issued the authoritative conversation
        identity.  Callers must publish the returned record to the client before
        starting work that writes messages, runs, artifacts, or memories.
        """
        if session.db_id:
            return None
        persisted = create_session(
            system_prompt=session.system_prompt,
            mode=session.mode,
            model_id=session.model_id,
            mode_config=session.mode_config,
            reasoning_effort=session.reasoning_effort or "",
            workspace_id=session.workspace_id,
            owner_id=session.owner_id,
            default_to_current_workspace=False,
        )
        session.db_id = str(persisted["id"])
        return persisted

    def create_persisted_once(
        self, session: DaoSession, *, request_key: str = "", title: str = "新对话",
        mode: str = DEFAULT_MODE, model_id: str = "", mode_config: dict[str, Any] | None = None,
        reasoning_effort: str = "",
    ) -> tuple[dict[str, Any] | None, bool]:
        """Create one empty conversation for one client intent.

        The browser can retry a WebSocket packet after reconnecting. A short
        request key makes that retry return the existing record instead of
        manufacturing another blank conversation.
        """
        key = str(request_key or "").strip()
        if key:
            previous_id = self._persisted_create_requests.get(key)
            if previous_id:
                previous = get_session(previous_id, owner_id=session.owner_id)
                if previous is not None:
                    session.pending_session_create_key = key
                    session.db_id = previous_id
                    return previous, False
        if key and session.pending_session_create_key == key and session.db_id:
            return get_session(session.db_id, owner_id=session.owner_id), False
        persisted = create_session(
            title=title or "新对话", mode=normalize_mode(mode), model_id=model_id,
            mode_config=mode_config, reasoning_effort=reasoning_effort or "",
            workspace_id=session.workspace_id, owner_id=session.owner_id,
            default_to_current_workspace=False,
        )
        session.pending_session_create_key = key or None
        session.db_id = str(persisted["id"])
        if key:
            self._persisted_create_requests[key] = session.db_id
        return persisted, True

    def get(self, session_id: str) -> DaoSession | None:
        return self._sessions.get(session_id)

    def attach_websocket(self, session: DaoSession, websocket: Any) -> None:
        """Register the control channel owned by one runtime session."""
        if self._sessions.get(session.id) is session:
            self._websockets[session.id] = websocket

    def websocket_items(self) -> list[tuple[DaoSession, Any]]:
        """Return a stable snapshot so broadcasts tolerate disconnects."""
        return [
            (session, self._websockets[runtime_id])
            for runtime_id, session in tuple(self._sessions.items())
            if runtime_id in self._websockets
        ]

    def active_run_owners(self) -> list[DaoSession]:
        """Return every in-process Run owner, including disconnected owners.

        A WebSocket can disappear before provider cancellation and durable
        settlement finish.  ``discard`` removes that runtime from the connected
        catalog, but the process-wide persistence barrier must still freeze
        shared model/Skill/MCP configuration until the Run actually settles.
        """
        owners: list[DaoSession] = []
        seen: set[int] = set()
        for session in self._sessions.values():
            if session.active_run_task is not None and id(session) not in seen:
                owners.append(session)
                seen.add(id(session))
        for session, _task in _active_persisted_runs.values():
            if id(session) not in seen:
                owners.append(session)
                seen.add(id(session))
        return owners

    def next_model_repository_revision(self) -> int:
        self._model_repository_revision += 1
        return self._model_repository_revision

    @property
    def session_catalog_revision(self) -> int:
        return self._session_catalog_revision

    def next_session_catalog_revision(self) -> int:
        self._session_catalog_revision += 1
        return self._session_catalog_revision

    def next_skills_revision(self) -> int:
        self._skills_revision += 1
        return self._skills_revision

    def next_extensions_revision(self) -> int:
        self._extensions_revision += 1
        return self._extensions_revision

    @property
    def extensions_revision(self) -> int:
        return self._extensions_revision

    def discard(self, session: DaoSession) -> None:
        """Release a disconnected runtime owner without touching SQLite."""
        if self._sessions.get(session.id) is session:
            self._sessions.pop(session.id, None)
        self._websockets.pop(session.id, None)


def start_session_run(
    session: DaoSession,
    coroutine: Any,
    *,
    release_guard: Callable[[str], bool] | None = None,
    on_settled: Callable[[str], Awaitable[None]] | None = None,
) -> bool:
    """Start one Run and release ownership only after durable settlement.

    ``release_guard`` is synchronous so the SQLite fact is checked immediately
    before the in-memory admission barrier is removed. A false result or an
    exception deliberately leaves the completed task installed as a fail-closed
    barrier. Once the owned task has actually completed, a few bounded delayed
    checks tolerate a transient SQLite visibility/read failure; persistent
    ledger inconsistency remains blocked for process-start repair.
    """
    if session.active_run_task is not None:
        if hasattr(coroutine, "close"):
            coroutine.close()
        return False
    persistence_key = str(session.db_id or "")
    if persistence_key:
        active = _active_persisted_runs.get(persistence_key)
        if active is not None:
            if hasattr(coroutine, "close"):
                coroutine.close()
            return False
    task: asyncio.Task[Any]
    settlement_retry_run_id: str | None = None

    def release_guard_allows(settled_run_id: str) -> bool:
        if release_guard is None:
            return True
        try:
            return bool(release_guard(settled_run_id))
        except Exception:
            logger.exception(
                "run settlement release guard failed for run=%s",
                settled_run_id,
            )
            return False

    def release_owner() -> bool:
        """Atomically release this exact in-memory ownership generation."""
        if session.active_run_task is not task:
            return False
        if (
            persistence_key
            and _active_persisted_runs.get(persistence_key) != (session, task)
        ):
            return False
        session.active_run_task = None
        session.active_run_session_id = None
        session.active_run_id = None
        if persistence_key:
            _active_persisted_runs.pop(persistence_key, None)
        return True

    async def release_and_notify(settled_run_id: str) -> bool:
        if not release_owner():
            return False
        if on_settled is not None:
            try:
                await on_settled(settled_run_id)
            except Exception:
                logger.debug("run settlement notification failed", exc_info=True)
        return True

    async def recheck_durable_settlement(settled_run_id: str) -> None:
        current = asyncio.current_task()
        try:
            for delay in _SETTLEMENT_RECHECK_DELAYS_SECONDS:
                # sleep() is also the cancellation point used during event-loop
                # shutdown. Even a test delay of zero yields until the owned
                # task's done callbacks have finished.
                await asyncio.sleep(max(0.0, float(delay)))
                if session.active_run_task is not task:
                    return
                if release_guard_allows(settled_run_id):
                    # Drop the session-level generation marker *before* the
                    # notification awaits. Releasing ownership permits a new
                    # Run immediately; that Run must be able to schedule its
                    # own rechecker even while this callback is still sending.
                    if session.settlement_recheck_task is current:
                        session.settlement_recheck_task = None
                    await release_and_notify(settled_run_id)
                    return
            logger.error(
                "run ownership retained after durable settlement rechecks: "
                "run=%s session=%s attempts=%d",
                settled_run_id,
                persistence_key,
                len(_SETTLEMENT_RECHECK_DELAYS_SECONDS),
            )
        finally:
            if session.settlement_recheck_task is current:
                session.settlement_recheck_task = None

    def schedule_settlement_recheck(settled_run_id: str) -> None:
        # The callback is attached to ``task`` itself, so this function runs
        # only after the owner is completed. At most one finite rechecker is
        # retained by the session, and its finally block drops that reference.
        if session.active_run_task is not task:
            return
        previous = session.settlement_recheck_task
        if previous is not None and not previous.done():
            return
        rechecker = asyncio.create_task(recheck_durable_settlement(settled_run_id))
        session.settlement_recheck_task = rechecker

    async def owned_run() -> Any:
        nonlocal settlement_retry_run_id
        result: Any = None
        try:
            result = await coroutine
            return result
        finally:
            settled_run_id = str(
                session.active_run_id
                or getattr(result, "run_id", "")
                or getattr(session.active_controller, "run_id", "")
                or ""
            )
            if release_guard_allows(settled_run_id):
                await release_and_notify(settled_run_id)
            else:
                settlement_retry_run_id = settled_run_id
                logger.error(
                    "run ownership retained because durable settlement is not terminal: run=%s session=%s",
                    settled_run_id, persistence_key,
                )

    task = asyncio.create_task(owned_run())
    session.active_run_task = task
    session.active_run_session_id = session.db_id
    if persistence_key:
        _active_persisted_runs[persistence_key] = (session, task)

    def clear_completed(completed: asyncio.Task[Any]) -> None:
        try:
            completed.result()
        except asyncio.CancelledError:
            logger.info("agent run cancelled")
        except Exception:
            logger.exception("agent run crashed")
        finally:
            if settlement_retry_run_id is not None:
                schedule_settlement_recheck(settlement_retry_run_id)

    task.add_done_callback(clear_completed)
    return True


def active_run_owner(session_id: str) -> DaoSession | None:
    """Return the live owner of a persisted conversation run, if any."""
    active = _active_persisted_runs.get(str(session_id or ""))
    if active is None:
        return None
    owner, _task = active
    return owner
