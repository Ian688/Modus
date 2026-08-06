"""Fail-closed ownership contract for Desktop background Run settlement.

The runner finishing is not itself proof that a Run finished durably.  These
tests exercise only local state and fakes; no provider or model is started.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


class RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, packet: dict) -> None:
        self.sent.append(packet)


def _force_release_test_owner(session) -> None:
    """Keep a deliberately fail-closed test barrier from leaking to other tests."""
    from modus.desktop import session_state

    rechecker = session.settlement_recheck_task
    if rechecker is not None and not rechecker.done():
        rechecker.cancel()
    session_state._active_persisted_runs.pop(session.db_id, None)
    session.active_run_task = None
    session.active_run_session_id = None
    session.active_run_id = None


async def _await_rechecker(session) -> None:
    """Wait for the finite settlement rechecker without racing its callback."""
    for _ in range(20):
        rechecker = session.settlement_recheck_task
        if rechecker is not None:
            await asyncio.wait_for(asyncio.shield(rechecker), timeout=2)
            return
        await asyncio.sleep(0)
    raise AssertionError("settlement rechecker was not scheduled")


async def _run_to_callback(server, session, websocket) -> asyncio.Task:
    run_id = str(session.active_run_id)

    async def completed_worker():
        return SimpleNamespace(run_id=run_id)

    assert server.start_session_run(
        session,
        completed_worker(),
        release_guard=server._run_release_guard(session),
        on_settled=server._run_settlement_callback(websocket, session),
    ) is True
    task = session.active_run_task
    assert task is not None
    await asyncio.wait_for(asyncio.shield(task), timeout=2)
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "durable_run",
    [
        None,
        {"run_id": "run-not-durable", "session_id": "db-not-durable", "state": "running"},
        {"run_id": "run-not-durable", "session_id": "another-session", "state": "completed"},
    ],
    ids=["missing-run", "nonterminal-run", "wrong-session"],
)
async def test_worker_completion_without_durable_terminal_keeps_owner_and_blocks_next_run(
    monkeypatch, durable_run,
):
    from modus.desktop import server
    from modus.desktop.session_state import active_run_owner

    session = server.DaoSession(id="runtime-not-durable", db_id="db-not-durable")
    session.active_run_id = "run-not-durable"
    websocket = RecordingWebSocket()
    monkeypatch.setattr(server, "get_run", lambda _run_id: durable_run)
    monkeypatch.setattr(server, "get_run_task", lambda _task_id: None)
    monkeypatch.setattr(server, "get_run_events_since", lambda _run_id, _since: [])

    try:
        owned_task = await _run_to_callback(server, session, websocket)

        assert owned_task.done() is True
        assert websocket.sent == []
        assert session.active_run_task is owned_task
        assert session.active_run_session_id == session.db_id
        assert session.active_run_id == "run-not-durable"
        assert active_run_owner(session.db_id) is session

        async def forbidden_second_run():
            raise AssertionError("a second Run entered through an unsettled durable barrier")

        assert server.start_session_run(session, forbidden_second_run()) is False
        assert session.active_run_task is owned_task
        assert active_run_owner(session.db_id) is session

        competing_window = server.DaoSession(
            id="runtime-not-durable-observer", db_id=session.db_id,
        )
        assert server.start_session_run(competing_window, forbidden_second_run()) is False
        assert competing_window.active_run_task is None
        assert active_run_owner(session.db_id) is session
    finally:
        _force_release_test_owner(session)


@pytest.mark.asyncio
async def test_durable_state_read_failure_keeps_owner_and_suppresses_settlement(
    monkeypatch,
):
    from modus.desktop import server
    from modus.desktop.session_state import active_run_owner

    session = server.DaoSession(id="runtime-read-failed", db_id="db-read-failed")
    session.active_run_id = "run-read-failed"
    websocket = RecordingWebSocket()

    def fail_read(_run_id: str):
        raise OSError("simulated SQLite read failure")

    monkeypatch.setattr(server, "get_run", fail_read)
    monkeypatch.setattr(server, "get_run_task", lambda _task_id: None)
    monkeypatch.setattr(server, "get_run_events_since", lambda _run_id, _since: [])

    try:
        owned_task = await _run_to_callback(server, session, websocket)

        assert websocket.sent == []
        assert session.active_run_task is owned_task
        assert session.active_run_id == "run-read-failed"
        assert active_run_owner(session.db_id) is session
        assert server.start_session_run(session, asyncio.sleep(0)) is False
    finally:
        _force_release_test_owner(session)


@pytest.mark.asyncio
async def test_release_guard_exception_is_fail_closed_and_skips_settlement_callback():
    from modus.desktop import server
    from modus.desktop.session_state import active_run_owner

    session = server.DaoSession(id="runtime-callback-failed", db_id="db-callback-failed")
    session.active_run_id = "run-callback-failed"

    notifications: list[str] = []

    def release_guard_failed(_run_id: str) -> bool:
        raise RuntimeError("durable settlement guard crashed")

    async def record_settled(run_id: str) -> None:
        notifications.append(run_id)

    async def completed_worker():
        return SimpleNamespace(run_id="run-callback-failed")

    try:
        assert server.start_session_run(
            session,
            completed_worker(),
            release_guard=release_guard_failed,
            on_settled=record_settled,
        ) is True
        owned_task = session.active_run_task
        assert owned_task is not None
        await asyncio.wait_for(asyncio.shield(owned_task), timeout=2)

        assert session.active_run_task is owned_task
        assert session.active_run_session_id == session.db_id
        assert session.active_run_id == "run-callback-failed"
        assert active_run_owner(session.db_id) is session
        assert notifications == []
        assert server.start_session_run(session, asyncio.sleep(0)) is False
    finally:
        _force_release_test_owner(session)


@pytest.mark.asyncio
async def test_completed_owner_rechecks_transient_guard_failure_then_releases_once(
    monkeypatch,
):
    from modus.desktop import session_state
    from modus.desktop.session_state import active_run_owner

    monkeypatch.setattr(session_state, "_SETTLEMENT_RECHECK_DELAYS_SECONDS", (0, 0))
    session = session_state.DaoSession(
        id="runtime-transient-settlement", db_id="db-transient-settlement",
    )
    session.active_run_id = "run-transient-settlement"
    guard_results = iter([OSError("transient SQLite read failure"), True])
    guard_calls: list[str] = []
    notifications: list[str] = []

    def transient_guard(run_id: str) -> bool:
        guard_calls.append(run_id)
        try:
            result = next(guard_results)
        except StopIteration:
            pytest.fail("guard called after release")
        if isinstance(result, BaseException):
            raise result
        return bool(result)

    async def record_settled(run_id: str) -> None:
        notifications.append(run_id)

    async def completed_worker():
        return SimpleNamespace(run_id="run-transient-settlement")

    assert session_state.start_session_run(
        session, completed_worker(),
        release_guard=transient_guard,
        on_settled=record_settled,
    ) is True
    owned_task = session.active_run_task
    assert owned_task is not None
    await asyncio.wait_for(asyncio.shield(owned_task), timeout=2)

    # Initial denial is immediately fail-closed. The done callback schedules,
    # rather than performs, the delayed retry.
    assert session.active_run_task is owned_task
    assert active_run_owner(session.db_id) is session
    assert notifications == []

    await _await_rechecker(session)

    assert guard_calls == [
        "run-transient-settlement", "run-transient-settlement",
    ]
    assert notifications == ["run-transient-settlement"]
    assert session.active_run_task is None
    assert session.active_run_session_id is None
    assert session.active_run_id is None
    assert session.settlement_recheck_task is None
    assert active_run_owner(session.db_id) is None


@pytest.mark.asyncio
async def test_completed_owner_recheck_is_finite_for_inconsistent_ledger(monkeypatch):
    from modus.desktop import session_state
    from modus.desktop.session_state import active_run_owner

    delays = (0, 0, 0)
    monkeypatch.setattr(session_state, "_SETTLEMENT_RECHECK_DELAYS_SECONDS", delays)
    session = session_state.DaoSession(
        id="runtime-persistent-settlement", db_id="db-persistent-settlement",
    )
    session.active_run_id = "run-persistent-settlement"
    guard_calls: list[str] = []
    notifications: list[str] = []

    def inconsistent_guard(run_id: str) -> bool:
        guard_calls.append(run_id)
        return False

    async def completed_worker():
        return SimpleNamespace(run_id="run-persistent-settlement")

    try:
        assert session_state.start_session_run(
            session, completed_worker(),
            release_guard=inconsistent_guard,
            on_settled=lambda run_id: notifications.append(run_id),
        ) is True
        owned_task = session.active_run_task
        assert owned_task is not None
        await asyncio.wait_for(asyncio.shield(owned_task), timeout=2)
        await _await_rechecker(session)

        assert guard_calls == ["run-persistent-settlement"] * (1 + len(delays))
        assert notifications == []
        assert session.active_run_task is owned_task
        assert session.settlement_recheck_task is None
        assert active_run_owner(session.db_id) is session
        assert session_state.start_session_run(session, asyncio.sleep(0)) is False

        # Exhaustion does not silently start another polling cycle.
        await asyncio.sleep(0)
        assert guard_calls == ["run-persistent-settlement"] * (1 + len(delays))
    finally:
        _force_release_test_owner(session)


@pytest.mark.asyncio
async def test_completed_owner_recheck_can_be_cancelled_without_releasing(monkeypatch):
    from modus.desktop import session_state
    from modus.desktop.session_state import active_run_owner

    monkeypatch.setattr(session_state, "_SETTLEMENT_RECHECK_DELAYS_SECONDS", (60,))
    session = session_state.DaoSession(
        id="runtime-cancel-recheck", db_id="db-cancel-recheck",
    )
    session.active_run_id = "run-cancel-recheck"
    guard_calls: list[str] = []
    notifications: list[str] = []

    def denied_guard(run_id: str) -> bool:
        guard_calls.append(run_id)
        return False

    async def completed_worker():
        return SimpleNamespace(run_id="run-cancel-recheck")

    try:
        assert session_state.start_session_run(
            session, completed_worker(),
            release_guard=denied_guard,
            on_settled=lambda run_id: notifications.append(run_id),
        ) is True
        owned_task = session.active_run_task
        assert owned_task is not None
        await asyncio.wait_for(asyncio.shield(owned_task), timeout=2)
        for _ in range(20):
            rechecker = session.settlement_recheck_task
            if rechecker is not None:
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("settlement rechecker was not scheduled")

        rechecker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await rechecker

        assert guard_calls == ["run-cancel-recheck"]
        assert notifications == []
        assert session.settlement_recheck_task is None
        assert session.active_run_task is owned_task
        assert active_run_owner(session.db_id) is session
    finally:
        _force_release_test_owner(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("durable_state", ["completed", "failed", "cancelled", "interrupted"])
async def test_durable_terminal_state_releases_owner_and_broadcasts_settlement(
    monkeypatch, durable_state,
):
    from modus.desktop import server
    from modus.desktop.session_state import active_run_owner

    run_id = f"run-durable-{durable_state}"
    session = server.DaoSession(
        id=f"runtime-durable-{durable_state}",
        db_id=f"db-durable-{durable_state}",
    )
    session.active_run_id = run_id
    websocket = RecordingWebSocket()
    monkeypatch.setattr(server, "get_run", lambda _run_id: {
        "run_id": run_id,
        "session_id": session.db_id,
        "state": durable_state,
        "stop_reason": "process_restart" if durable_state == "interrupted" else durable_state,
    })
    root_status = "cancelled" if durable_state == "interrupted" else durable_state
    terminal_type = "run_completed" if durable_state == "completed" else "run_error"
    monkeypatch.setattr(server, "get_run_task", lambda _task_id: {
        "task_id": f"task_{run_id}_root",
        "run_id": run_id,
        "session_id": session.db_id,
        "task_kind": "root",
        "status": root_status,
    })
    monkeypatch.setattr(server, "get_run_events_since", lambda _run_id, _since: [{
        "event_id": f"evt-{run_id}",
        "run_id": run_id,
        "session_id": session.db_id,
        "task_id": f"task_{run_id}_root",
        "type": terminal_type,
        "status": root_status,
        "sequence": 1,
        "payload": {
            "stop_reason": (
                "process_restart" if durable_state == "interrupted" else durable_state
            ),
        },
    }])

    owned_task = await _run_to_callback(server, session, websocket)

    assert owned_task.done() is True
    assert [packet["type"] for packet in websocket.sent] == ["run_settled"]
    assert websocket.sent[0]["run_id"] == run_id
    assert websocket.sent[0]["run_owned_by_connection"] is True
    assert session.active_run_task is None
    assert session.active_run_session_id is None
    assert session.active_run_id is None
    assert active_run_owner(session.db_id) is None

    # A fresh Run can enter only after the durable terminal released the barrier.
    assert server.start_session_run(session, asyncio.sleep(0)) is True
    next_task = session.active_run_task
    assert next_task is not None
    await asyncio.wait_for(asyncio.shield(next_task), timeout=2)


def test_server_release_guard_requires_a_bound_run_id(monkeypatch):
    from modus.desktop import server

    session = server.DaoSession(id="runtime-empty-run", db_id="db-empty-run")
    reads: list[str] = []
    monkeypatch.setattr(
        server, "get_run", lambda run_id: reads.append(run_id) or pytest.fail(
            "an empty Run identity must not reach the durable ledger",
        ),
    )

    assert server._run_release_guard(session)("") is False
    assert reads == []


@pytest.mark.parametrize(
    ("run_state", "root_state", "terminal_type", "terminal_status"),
    [
        ("completed", "running", "run_completed", "completed"),
        ("completed", "completed", None, None),
        ("completed", "completed", "run_error", "failed"),
        ("failed", "failed", "run_completed", "completed"),
        ("cancelled", "cancelled", "run_error", "failed"),
    ],
    ids=[
        "root-nonterminal", "missing-terminal-event", "completed-with-error-event",
        "failed-with-completed-event", "cancelled-with-failed-status",
    ],
)
def test_release_guard_rejects_inconsistent_terminal_ledger(
    monkeypatch, run_state, root_state, terminal_type, terminal_status,
):
    from modus.desktop import server

    run_id = "run-inconsistent-ledger"
    session = server.DaoSession(id="runtime-inconsistent", db_id="db-inconsistent")
    monkeypatch.setattr(server, "get_run", lambda _run_id: {
        "run_id": run_id, "session_id": session.db_id,
        "state": run_state, "stop_reason": run_state,
    })
    monkeypatch.setattr(server, "get_run_task", lambda _task_id: {
        "task_id": f"task_{run_id}_root", "run_id": run_id,
        "session_id": session.db_id, "task_kind": "root", "status": root_state,
    })
    events = [] if terminal_type is None else [{
        "event_id": "evt-inconsistent", "run_id": run_id,
        "session_id": session.db_id, "task_id": f"task_{run_id}_root",
        "type": terminal_type, "status": terminal_status, "sequence": 1,
        "payload": {"stop_reason": run_state},
    }]
    monkeypatch.setattr(
        server, "get_run_events_since", lambda _run_id, _since: events,
    )

    assert server._run_release_guard(session)(run_id) is False


def test_release_guard_rejects_multiple_terminal_events(monkeypatch):
    from modus.desktop import server

    run_id = "run-double-terminal"
    session = server.DaoSession(id="runtime-double-terminal", db_id="db-double-terminal")
    monkeypatch.setattr(server, "get_run", lambda _run_id: {
        "run_id": run_id, "session_id": session.db_id,
        "state": "completed", "stop_reason": "completed",
    })
    monkeypatch.setattr(server, "get_run_task", lambda _task_id: {
        "task_id": f"task_{run_id}_root", "run_id": run_id,
        "session_id": session.db_id, "task_kind": "root", "status": "completed",
    })
    monkeypatch.setattr(server, "get_run_events_since", lambda _run_id, _since: [
        {
            "event_id": "evt-completed", "run_id": run_id,
            "session_id": session.db_id, "task_id": f"task_{run_id}_root",
            "type": "run_completed", "status": "completed", "sequence": 1,
            "payload": {"stop_reason": "completed"},
        },
        {
            "event_id": "evt-error", "run_id": run_id,
            "session_id": session.db_id, "task_id": f"task_{run_id}_root",
            "type": "run_error", "status": "failed", "sequence": 2,
            "payload": {"stop_reason": "failed"},
        },
    ])

    assert server._run_release_guard(session)(run_id) is False


def test_admission_failure_releases_without_agent_terminal_event(monkeypatch):
    from modus.desktop import server

    run_id = "run-admission-failed"
    session = server.DaoSession(id="runtime-admission-failed", db_id="db-admission-failed")
    monkeypatch.setattr(server, "get_run", lambda _run_id: {
        "run_id": run_id, "session_id": session.db_id,
        "state": "failed", "stop_reason": "admission_persistence_failed",
    })
    root = {
        "task_id": f"task_{run_id}_root", "run_id": run_id,
        "session_id": session.db_id, "task_kind": "root", "status": "failed",
    }
    monkeypatch.setattr(server, "get_run_task", lambda _task_id: root)
    monkeypatch.setattr(server, "get_run_events_since", lambda _run_id, _since: [])

    assert server._run_release_guard(session)(run_id) is True


def test_owned_admission_failure_without_root_stays_blocked(monkeypatch):
    from modus.desktop import server

    run_id = "run-admission-missing-root"
    session = server.DaoSession(id="runtime-admission-missing-root", db_id="db-admission-missing-root")
    monkeypatch.setattr(server, "get_run", lambda _run_id: {
        "run_id": run_id, "session_id": session.db_id,
        "state": "failed", "stop_reason": "admission_persistence_failed",
    })
    monkeypatch.setattr(server, "get_run_task", lambda _task_id: None)
    monkeypatch.setattr(server, "get_run_events_since", lambda _run_id, _since: [])

    assert server._run_release_guard(session)(run_id) is False


def test_admission_failure_with_nonfailed_root_stays_blocked(monkeypatch):
    from modus.desktop import server

    run_id = "run-admission-root-running"
    session = server.DaoSession(id="runtime-admission-root-running", db_id="db-admission-root-running")
    monkeypatch.setattr(server, "get_run", lambda _run_id: {
        "run_id": run_id, "session_id": session.db_id,
        "state": "failed", "stop_reason": "admission_transport_failed",
    })
    monkeypatch.setattr(server, "get_run_task", lambda _task_id: {
        "task_id": f"task_{run_id}_root", "run_id": run_id,
        "session_id": session.db_id, "task_kind": "root", "status": "running",
    })
    monkeypatch.setattr(server, "get_run_events_since", lambda _run_id, _since: [])

    assert server._run_release_guard(session)(run_id) is False


def test_admission_failure_with_agent_event_stays_blocked(monkeypatch):
    from modus.desktop import server

    run_id = "run-admission-with-event"
    session = server.DaoSession(id="runtime-admission-with-event", db_id="db-admission-with-event")
    monkeypatch.setattr(server, "get_run", lambda _run_id: {
        "run_id": run_id, "session_id": session.db_id,
        "state": "failed", "stop_reason": "admission_conflict",
    })
    monkeypatch.setattr(server, "get_run_task", lambda _task_id: {
        "task_id": f"task_{run_id}_root", "run_id": run_id,
        "session_id": session.db_id, "task_kind": "root", "status": "failed",
    })
    monkeypatch.setattr(server, "get_run_events_since", lambda _run_id, _since: [{
        "event_id": "evt-impossible-admission", "run_id": run_id,
        "session_id": session.db_id, "task_id": f"task_{run_id}_root",
        "type": "run_error", "status": "failed", "sequence": 1,
        "payload": {"stop_reason": "admission_conflict"},
    }])

    assert server._run_release_guard(session)(run_id) is False


@pytest.mark.parametrize("failed_reader", ["root", "events"])
def test_release_guard_ledger_read_failure_is_fail_closed(monkeypatch, failed_reader):
    from modus.desktop import server

    run_id = "run-secondary-read-failed"
    session = server.DaoSession(id="runtime-secondary-read-failed", db_id="db-secondary-read-failed")
    monkeypatch.setattr(server, "get_run", lambda _run_id: {
        "run_id": run_id, "session_id": session.db_id,
        "state": "completed", "stop_reason": "completed",
    })

    def fail_read(*_args):
        raise OSError("simulated secondary ledger read failure")

    monkeypatch.setattr(
        server, "get_run_task",
        fail_read if failed_reader == "root" else lambda _task_id: {},
    )
    monkeypatch.setattr(
        server, "get_run_events_since",
        fail_read if failed_reader == "events" else lambda _run_id, _since: [],
    )

    assert server._run_release_guard(session)(run_id) is False
