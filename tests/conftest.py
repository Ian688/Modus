"""Test-wide isolation for Modus's user-owned desktop persistence."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path
import sys
from typing import Any

import pytest
import pytest_asyncio


async def _drain_desktop_task(task: asyncio.Task[Any] | None) -> None:
    """Cancel and await a task on the event loop that owns it."""
    if task is None:
        return
    loop = task.get_loop()
    if loop.is_closed():
        if not task.done():
            raise RuntimeError("desktop task is still pending on a closed event loop")
        try:
            task.result()
        except BaseException:
            pass
        return

    async def cancel_and_wait() -> None:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Task done callbacks schedule the bounded settlement rechecker with
        # call_soon(). Yield once so teardown can observe that exact task.
        await asyncio.sleep(0)

    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - this helper is awaited in tests
        running_loop = None

    if loop.is_running():
        if running_loop is loop:
            await cancel_and_wait()
            return
        # A TestClient portal loop can live on another thread during teardown.
        # Both cancellation and gathering must happen on that owning thread.
        try:
            bridge = asyncio.run_coroutine_threadsafe(cancel_and_wait(), loop)
            await asyncio.wait_for(asyncio.wrap_future(bridge), timeout=2)
        except TimeoutError:
            bridge.cancel()
            raise RuntimeError("timed out draining desktop task on its owning loop")
        return

    # A non-running foreign loop cannot be nested in pytest-asyncio's current
    # loop. Drive it briefly on a worker thread so the task's finally blocks and
    # callbacks still complete before this fixture releases its state.
    await asyncio.to_thread(loop.run_until_complete, cancel_and_wait())


def _unique_managers(managers: Iterable[Any]) -> list[Any]:
    return list({id(manager): manager for manager in managers if manager is not None}.values())


async def _clear_desktop_runtime_state(*managers: Any) -> None:
    """Drain exact Run generations, then remove this test's runtime state."""
    session_state = sys.modules.get("modus.desktop.session_state")
    if session_state is None:
        return

    unique_managers = _unique_managers(managers)
    registry_entries = list(session_state._active_persisted_runs.items())
    manager_snapshots = [
        (
            manager,
            list(getattr(manager, "_sessions", {}).items()),
            list(getattr(manager, "_websockets", {}).items()),
            list(getattr(manager, "_persisted_create_requests", {}).items()),
        )
        for manager in unique_managers
    ]
    sessions = [session for _key, (session, _task) in registry_entries]
    for _manager, session_items, _websocket_items, _request_items in manager_snapshots:
        sessions.extend(session for _runtime_id, session in session_items)

    unique_sessions = list({id(session): session for session in sessions}.values())
    generations = [
        (
            session,
            getattr(session, "active_run_task", None),
            getattr(session, "active_controller", None),
        )
        for session in unique_sessions
    ]
    for _session, owner_task, _controller in generations:
        await _drain_desktop_task(owner_task)

    # Owner completion may install one bounded rechecker in its done callback.
    # Re-read after all owners have drained, then cancel/await that generation.
    recheckers = [
        (session, getattr(session, "settlement_recheck_task", None))
        for session, _owner_task, _controller in generations
    ]
    for _session, rechecker in recheckers:
        await _drain_desktop_task(rechecker)

    for session, owner_task, controller in generations:
        current_owner = getattr(session, "active_run_task", None)
        if current_owner is not None and current_owner is not owner_task:
            continue
        current_rechecker = getattr(session, "settlement_recheck_task", None)
        captured_rechecker = next(
            (task for candidate, task in recheckers if candidate is session), None,
        )
        if current_rechecker is captured_rechecker:
            session.settlement_recheck_task = None
        if current_owner is owner_task:
            session.active_run_task = None
            session.active_run_session_id = None
            session.active_run_id = None
        if getattr(session, "active_controller", None) is controller:
            session.active_controller = None

    for persistence_key, generation in registry_entries:
        if session_state._active_persisted_runs.get(persistence_key) == generation:
            session_state._active_persisted_runs.pop(persistence_key, None)
    for manager, session_items, websocket_items, request_items in manager_snapshots:
        runtime_sessions = getattr(manager, "_sessions", {})
        for runtime_id, session in session_items:
            if runtime_sessions.get(runtime_id) is session:
                runtime_sessions.pop(runtime_id, None)
        websockets = getattr(manager, "_websockets", {})
        for runtime_id, websocket in websocket_items:
            if websockets.get(runtime_id) is websocket:
                websockets.pop(runtime_id, None)
        requests = getattr(manager, "_persisted_create_requests", {})
        for request_key, db_id in request_items:
            if requests.get(request_key) == db_id:
                requests.pop(request_key, None)


@pytest_asyncio.fixture(autouse=True)
async def isolate_desktop_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Never let a test read or mutate ``~/.modus/desktop.db``."""
    from modus.desktop import db

    server = sys.modules.get("modus.desktop.server")
    manager_at_setup = getattr(server, "manager", None)
    data_dir = tmp_path / "modus-data"
    monkeypatch.setattr(db, "DB_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "desktop.db")
    db.init_db()
    yield

    # Some contract tests deliberately retain a fail-closed owner.  Let those
    # tests inspect it, then ensure neither its task nor its bounded settlement
    # rechecker can affect the next test's global mutation guards.
    server = sys.modules.get("modus.desktop.server")
    manager_at_teardown = getattr(server, "manager", None)
    await _clear_desktop_runtime_state(manager_at_setup, manager_at_teardown)
