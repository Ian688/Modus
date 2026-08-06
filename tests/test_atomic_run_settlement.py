from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest


def _terminal_event(
    *, event_id: str, run_id: str, task_id: str, event_type: str,
    sequence: int, stop_reason: str,
) -> dict:
    failed = event_type == "run_error"
    payload = {
        "stop_reason": stop_reason,
        "budget": {"total_tokens": 9 if failed else 17},
    }
    if failed:
        payload.update({
            "code": "provider_failed",
            "message": "provider failed after the competing terminal",
            "retryable": True,
        })
    return {
        "event_id": event_id,
        "run_id": run_id,
        "channel_id": "user_host",
        "parent_event_id": None,
        "sequence": sequence,
        "timestamp": f"2026-08-03T00:00:0{sequence}.000Z",
        "mode": "default",
        "actor": {"kind": "system", "id": "system", "label": "system"},
        "type": event_type,
        "status": "failed" if failed else "completed",
        "payload": payload,
        "task_id": task_id,
        "revision": 0,
    }


def _create_running_run(db, *, session_id: str, run_id: str, task_id: str) -> None:
    db.create_run(run_id, session_id, "default")
    db.create_run_task(
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        ordinal=-1,
        task_kind="root",
        title="User task",
    )
    assert db.update_run_task(task_id, status="running") is True


def test_settle_run_event_concurrent_opposite_terminals_commit_one_consistent_fact(
    monkeypatch, tmp_path,
):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("concurrent terminal settlement")
    run_id = "run-terminal-race"
    task_id = f"task_{run_id}_root"
    _create_running_run(db, session_id=session["id"], run_id=run_id, task_id=task_id)

    completed = _terminal_event(
        event_id="evt-completed", run_id=run_id, task_id=task_id,
        event_type="run_completed", sequence=1, stop_reason="completed",
    )
    failed = _terminal_event(
        event_id="evt-failed", run_id=run_id, task_id=task_id,
        event_type="run_error", sequence=2, stop_reason="engine_error",
    )
    barrier = Barrier(2)

    def settle(event: dict) -> tuple[dict, bool]:
        barrier.wait(timeout=5)
        return event, db.settle_run_event(session["id"], event)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(settle, (completed, failed)))

    assert sorted(claimed for _event, claimed in outcomes) == [False, True]
    winner = next(event for event, claimed in outcomes if claimed)
    expected_state = "completed" if winner["type"] == "run_completed" else "failed"

    run = db.get_run(run_id)
    root = db.get_run_task(task_id)
    events = db.get_run_events(run_id)
    assert run is not None and root is not None
    assert (run["state"], run["stop_reason"]) == (
        expected_state, winner["payload"]["stop_reason"],
    )
    assert run["budget"] == winner["payload"]["budget"]
    assert root["status"] == expected_state
    assert len(events) == 1
    assert (events[0]["event_id"], events[0]["type"], events[0]["task_id"]) == (
        winner["event_id"], winner["type"], task_id,
    )


def test_settle_run_event_completed_then_late_error_is_a_total_noop(
    monkeypatch, tmp_path,
):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("late terminal")
    run_id = "run-late-terminal"
    task_id = f"task_{run_id}_root"
    _create_running_run(db, session_id=session["id"], run_id=run_id, task_id=task_id)

    completed = _terminal_event(
        event_id="evt-first-completed", run_id=run_id, task_id=task_id,
        event_type="run_completed", sequence=1, stop_reason="completed",
    )
    late_error = _terminal_event(
        event_id="evt-late-error", run_id=run_id, task_id=task_id,
        event_type="run_error", sequence=2, stop_reason="engine_error",
    )

    assert db.settle_run_event(session["id"], completed) is True
    committed_run = db.get_run(run_id)
    committed_root = db.get_run_task(task_id)
    committed_events = db.get_run_events(run_id)

    assert db.settle_run_event(session["id"], late_error) is False
    assert db.get_run(run_id) == committed_run
    assert db.get_run_task(task_id) == committed_root
    assert db.get_run_events(run_id) == committed_events
    assert committed_run is not None and committed_run["state"] == "completed"
    assert committed_root is not None and committed_root["status"] == "completed"
    assert [event["type"] for event in committed_events] == ["run_completed"]


def test_settle_run_event_without_canonical_root_is_a_total_noop(
    monkeypatch, tmp_path,
):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("missing root")
    run_id = "run-missing-root"
    db.create_run(run_id, session["id"], "default")
    event = _terminal_event(
        event_id="evt-missing-root", run_id=run_id,
        task_id=f"task_{run_id}_root", event_type="run_completed",
        sequence=1, stop_reason="completed",
    )

    before = db.get_run(run_id)
    assert db.settle_run_event(session["id"], event) is False
    assert db.get_run(run_id) == before
    assert before is not None and before["state"] == "running"
    assert db.get_run_events(run_id) == []


def test_settle_run_event_rejects_worker_task_id_without_mutating_ledger(
    monkeypatch, tmp_path,
):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("worker terminal")
    run_id = "run-worker-terminal"
    root_id = f"task_{run_id}_root"
    worker_id = f"task_{run_id}_worker"
    _create_running_run(
        db, session_id=session["id"], run_id=run_id, task_id=root_id,
    )
    db.create_run_task(
        task_id=worker_id, run_id=run_id, session_id=session["id"], ordinal=0,
        parent_task_id=root_id, task_kind="worker", title="Worker",
    )
    event = _terminal_event(
        event_id="evt-worker-terminal", run_id=run_id, task_id=worker_id,
        event_type="run_error", sequence=1, stop_reason="engine_error",
    )
    before_run = db.get_run(run_id)
    before_root = db.get_run_task(root_id)
    before_worker = db.get_run_task(worker_id)

    assert db.settle_run_event(session["id"], event) is False
    assert db.get_run(run_id) == before_run
    assert db.get_run_task(root_id) == before_root
    assert db.get_run_task(worker_id) == before_worker
    assert db.get_run_events(run_id) == []


@pytest.mark.parametrize(
    ("root_status", "event_type", "stop_reason"),
    [
        ("failed", "run_completed", "completed"),
        ("completed", "run_error", "engine_error"),
    ],
)
def test_settle_run_event_rejects_conflicting_terminal_root_as_total_noop(
    monkeypatch, tmp_path, root_status, event_type, stop_reason,
):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("conflicting root terminal")
    run_id = f"run-root-{root_status}-before-{event_type}"
    root_id = f"task_{run_id}_root"
    _create_running_run(
        db, session_id=session["id"], run_id=run_id, task_id=root_id,
    )
    assert db.update_run_task(root_id, status=root_status) is True
    event = _terminal_event(
        event_id=f"evt-root-{root_status}-before-{event_type}", run_id=run_id,
        task_id=root_id, event_type=event_type, sequence=1,
        stop_reason=stop_reason,
    )
    before_run = db.get_run(run_id)
    before_root = db.get_run_task(root_id)

    assert db.settle_run_event(session["id"], event) is False
    assert db.get_run(run_id) == before_run
    assert db.get_run_task(root_id) == before_root
    assert before_run is not None and before_run["state"] == "running"
    assert db.get_run_events(run_id) == []


@pytest.mark.parametrize(
    ("event_type", "stop_reason", "status"),
    [
        ("run_completed", "engine_error", "completed"),
        ("run_completed", "completed", "failed"),
        ("run_error", "cancelled", "failed"),
        ("run_error", "engine_error", "cancelled"),
        ("run_error", "engine_error", "completed"),
    ],
)
def test_settle_run_event_rejects_inconsistent_terminal_envelope(
    monkeypatch, tmp_path, event_type, stop_reason, status,
):
    from modus.desktop import db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "desktop.db")
    db.init_db()
    session = db.create_session("invalid terminal envelope")
    run_id = f"run-invalid-{event_type}-{stop_reason}-{status}"
    root_id = f"task_{run_id}_root"
    _create_running_run(
        db, session_id=session["id"], run_id=run_id, task_id=root_id,
    )
    event = _terminal_event(
        event_id=f"evt-invalid-{event_type}-{stop_reason}-{status}",
        run_id=run_id, task_id=root_id, event_type=event_type,
        sequence=1, stop_reason=stop_reason,
    )
    event["status"] = status
    before_run = db.get_run(run_id)
    before_root = db.get_run_task(root_id)

    assert db.settle_run_event(session["id"], event) is False
    assert db.get_run(run_id) == before_run
    assert db.get_run_task(root_id) == before_root
    assert db.get_run_events(run_id) == []


@pytest.mark.parametrize("event_type", ["run_completed", "run_error"])
def test_server_terminal_audit_routes_through_atomic_settlement(
    monkeypatch, event_type,
):
    from modus.desktop import server

    session = server.DaoSession(id="runtime-audit", db_id="session-audit")
    event = {
        "event_id": "evt-terminal-audit",
        "run_id": "run-terminal-audit",
        "mode": "default",
        "type": event_type,
    }
    settled: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        server, "get_session",
        lambda session_id, **kwargs: {"id": session_id},
    )
    monkeypatch.setattr(server, "create_run", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        server, "settle_run_event",
        lambda session_id, audited: settled.append((session_id, audited)) or True,
    )
    monkeypatch.setattr(
        server, "upsert_run_event",
        lambda *_args, **_kwargs: pytest.fail("terminal audit used non-atomic upsert"),
    )
    monkeypatch.setattr(server, "build_workbench_run", lambda *_args: None)

    assert server._audit_event_for(session)(event) is True
    assert settled == [(session.db_id, event)]


@pytest.mark.asyncio
async def test_emitter_locks_audited_terminal_even_when_its_transport_send_fails():
    from modus.desktop.events import Actor, ChannelId, EventType, RunEventEmitter

    audited: list[dict] = []
    send_attempts: list[dict] = []

    def audit_event(event: dict) -> bool:
        audited.append(event)
        return True

    async def fail_first_send(packet: dict) -> None:
        send_attempts.append(packet)
        if len(send_attempts) == 1:
            raise ConnectionError("browser disconnected after durable settlement")

    emitter = RunEventEmitter(
        run_id="run-send-failure",
        mode="default",
        send_json=fail_first_send,
        audit_event=audit_event,
        root_task_id="task-send-failure-root",
    )

    with pytest.raises(ConnectionError, match="browser disconnected"):
        await emitter.emit(
            EventType.RUN_COMPLETED,
            ChannelId.USER_HOST,
            Actor.system(),
            {"stop_reason": "completed", "budget": {}},
        )

    suppressed = await emitter.emit(
        EventType.RUN_ERROR,
        ChannelId.USER_HOST,
        Actor.system(),
        {"stop_reason": "engine_error", "message": "late transport error"},
    )

    assert suppressed.type is EventType.RUN_COMPLETED
    assert suppressed.event_id == audited[0]["event_id"]
    assert [event["type"] for event in audited] == ["run_completed"]
    assert [packet["event"]["type"] for packet in send_attempts] == ["run_completed"]
