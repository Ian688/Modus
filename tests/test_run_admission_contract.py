"""Durable, idempotent admission contract for explicit Desktop Run requests.

These tests stop at the admission/runner boundary.  They use fake runners and
never construct a provider client or send a real model request.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest


class RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, packet: dict) -> None:
        self.sent.append(packet)


class RejectAdmissionAckWebSocket(RecordingWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.ack_attempts = 0

    async def send_json(self, packet: dict) -> None:
        if packet["type"] == "run_accepted":
            self.ack_attempts += 1
            raise ConnectionError("injected run admission transport failure")
        await super().send_json(packet)


def _explicit_message(session, *, request_id: str = "request-one", content: str = "build it") -> dict:
    return {
        "type": "run_message",
        "content": content,
        "request_id": request_id,
        "db_id": str(session.db_id or ""),
        "session_id": str(session.db_id or ""),
        "runtime_session_id": session.id,
    }


def _fresh_session(monkeypatch, server):
    from modus.desktop.session_state import SessionManager

    manager = SessionManager()
    monkeypatch.setattr(server, "manager", manager)
    return manager.create(engine=object(), workspace_root=str(server.Path.cwd()))


@pytest.mark.asyncio
async def test_projectless_default_run_uses_tool_free_engine(monkeypatch) -> None:
    from modus.desktop import server
    from modus.desktop.session_state import SessionManager

    manager = SessionManager()
    monkeypatch.setattr(server, "manager", manager)
    session = manager.create(engine=None)
    websocket = RecordingWebSocket()

    class ProjectlessEngine:
        config = server.load_config()
        tool_registry = server.ToolRegistry()

    async def fake_build(*_args, **_kwargs):
        return ProjectlessEngine()

    async def fake_runner(_ws, runner_session, _content, *args, **kwargs):
        return kwargs["emitter"]

    monkeypatch.setattr(server, "_build_session_engine", fake_build)
    monkeypatch.setattr(server, "_stream_to_ws", fake_runner)

    handled = await server._handle_explicit_run_message(
        websocket, session, _explicit_message(session),
    )

    assert handled is True
    assert any(packet.get("type") == "run_accepted" for packet in websocket.sent)
    assert session.db_id != ""
    assert session.engine.tool_registry.list_names() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "runner_name"),
    [
        ("default", "_stream_to_ws"),
        ("moa", "_run_moa_session"),
        ("peri", "_run_peri_session"),
    ],
)
async def test_explicit_admission_persists_then_acks_before_every_mode_runner_event(
    monkeypatch, mode: str, runner_name: str,
) -> None:
    from modus.desktop import server
    from modus.desktop.db import get_run, get_run_by_client_request_id, get_run_task
    from modus.desktop.events import Actor, ChannelId, EventStatus, EventType
    from modus.runtime.state import RunState

    websocket = RecordingWebSocket()
    session = _fresh_session(monkeypatch, server)
    session.mode = mode
    observed: dict[str, object] = {}

    async def fake_runner(ws, runner_session, content, *args, **kwargs):
        emitter = kwargs["emitter"]
        controller = kwargs["controller"]
        observed.update(
            emitter=emitter,
            controller=controller,
            run=get_run(emitter.run_id),
            root=get_run_task(f"task_{emitter.run_id}_root"),
            packets_before_runner=list(websocket.sent),
        )
        assert ws is websocket
        assert runner_session is session
        assert content == "build it"
        assert kwargs["persisted_run"] is True
        assert emitter.run_id == controller.run_id
        assert controller.state is RunState.RUNNING
        await emitter.emit(
            EventType.RUN_STARTED,
            ChannelId.USER_HOST,
            Actor.system(),
            {"state": "running", "mode": mode},
            status=EventStatus.STARTED,
        )
        await emitter.emit(
            EventType.RUN_COMPLETED,
            ChannelId.USER_HOST,
            Actor.host("primary", "主持人"),
            {"stop_reason": "completed", "budget": {}},
        )
        controller.transition(RunState.COMPLETED)
        if session.active_controller is controller:
            session.active_controller = None
        return emitter

    monkeypatch.setattr(server, runner_name, fake_runner)

    handled = await server._handle_explicit_run_message(
        websocket, session, _explicit_message(session),
    )
    task = session.active_run_task
    assert handled is True
    assert task is not None
    await task

    accepted = next(packet for packet in websocket.sent if packet["type"] == "run_accepted")
    first_event = next(packet for packet in websocket.sent if packet["type"] == "agent_event")
    assert websocket.sent.index(accepted) < websocket.sent.index(first_event)
    assert accepted["duplicate"] is False
    assert accepted["state"] == "running"
    assert accepted["requested_db_id"] == ""
    assert accepted["db_id"] == session.db_id
    assert accepted["runtime_session_id"] == session.id
    assert accepted["run_id"] == first_event["event"]["run_id"]

    persisted = observed["run"]
    root = observed["root"]
    packets_before_runner = observed["packets_before_runner"]
    assert isinstance(persisted, dict) and persisted["run_id"] == accepted["run_id"]
    assert persisted["session_id"] == session.db_id
    assert persisted["client_request_id"] == "request-one"
    assert isinstance(root, dict) and root["status"] == "running"
    assert [packet["type"] for packet in packets_before_runner] == [
        "session_persisted", "run_accepted",
    ]
    assert get_run_by_client_request_id("request-one")["run_id"] == accepted["run_id"]
    assert observed["emitter"].run_id == observed["controller"].run_id == accepted["run_id"]
    assert session.active_run_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default", "moa", "peri"])
async def test_legacy_run_message_gets_server_identity_and_uses_one_admission_path(
    monkeypatch, mode: str,
) -> None:
    from modus.desktop import server
    from modus.desktop.db import get_run, get_run_by_client_request_id, get_run_task
    from modus.desktop.events import Actor, ChannelId, EventType
    from modus.runtime.state import RunState

    websocket = RecordingWebSocket()
    session = _fresh_session(monkeypatch, server)
    session.mode = mode
    provider_starts = 0

    async def fake_submission(
        ws, runner_session, content, skill_id, *, mode, emitter, controller,
    ):
        nonlocal provider_starts
        provider_starts += 1
        assert ws is websocket
        assert runner_session is session
        assert content == "legacy task"
        assert skill_id == "legacy-skill"
        assert controller.state is RunState.RUNNING
        run = get_run(emitter.run_id)
        root = get_run_task(f"task_{emitter.run_id}_root")
        assert run is not None and run["state"] == "running"
        assert root is not None and root["status"] == "running"
        assert [packet["type"] for packet in websocket.sent] == [
            "session_persisted", "run_accepted",
        ]
        await emitter.emit(
            EventType.RUN_STARTED, ChannelId.USER_HOST, Actor.system(),
            {"state": "running", "mode": mode},
        )
        await emitter.emit(
            EventType.RUN_COMPLETED, ChannelId.USER_HOST,
            Actor.host("primary", "主持人"),
            {"stop_reason": "completed", "budget": {}},
        )
        controller.transition(RunState.COMPLETED)
        if session.active_controller is controller:
            session.active_controller = None
        return emitter

    monkeypatch.setattr(server, "_run_preallocated_submission", fake_submission)
    handled = await server._handle_explicit_run_message(
        websocket, session,
        {
            "type": "run_message", "content": "legacy task",
            "skill_id": "legacy-skill",
        },
    )
    task = session.active_run_task
    assert handled is True
    assert task is not None
    await task

    accepted = next(packet for packet in websocket.sent if packet["type"] == "run_accepted")
    assert accepted["request_id"].startswith("server-run-")
    assert accepted["requested_db_id"] == ""
    assert accepted["runtime_session_id"] == session.id
    assert accepted["db_id"] == session.db_id
    assert accepted["duplicate"] is False
    assert provider_starts == 1
    run = get_run_by_client_request_id(accepted["request_id"])
    assert run is not None and run["run_id"] == accepted["run_id"]
    assert run["mode"] == mode


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_point",
    ["atomic_raises", "atomic_returns_empty"],
)
async def test_atomic_run_root_persistence_failure_rejects_before_provider_start(
    monkeypatch, failure_point: str,
) -> None:
    from modus.desktop import server
    from modus.desktop.db import get_run_by_client_request_id

    websocket = RecordingWebSocket()
    session = _fresh_session(monkeypatch, server)
    provider_starts = 0

    async def forbidden_submission(*args, **kwargs):
        nonlocal provider_starts
        provider_starts += 1
        raise AssertionError("failed admission must not enter provider code")

    monkeypatch.setattr(server, "_run_preallocated_submission", forbidden_submission)
    def fail_atomic(*_args, **_kwargs):
        if failure_point == "atomic_raises":
            raise sqlite3.OperationalError("injected atomic admission failure")
        return {}

    monkeypatch.setattr(server, "create_run_admission", fail_atomic)

    request = _explicit_message(
        session, request_id=f"root-failure-{failure_point}",
    )
    handled = await server._handle_explicit_run_message(
        websocket, session, request,
    )

    assert handled is True
    assert provider_starts == 0
    assert session.active_run_task is None
    assert session.active_controller is None
    assert session.active_run_id is None
    assert not any(packet["type"] == "run_accepted" for packet in websocket.sent)
    assert [packet["type"] for packet in websocket.sent] == [
        "session_persisted", "error",
    ]
    error = websocket.sent[-1]
    assert error["code"] == "run_admission_failed"
    assert error["request_id"] == f"root-failure-{failure_point}"
    assert error["db_id"] == session.db_id

    # Atomic Run/root creation rolls the whole transaction back.  There is no
    # orphan request reservation to replay or repair.
    assert get_run_by_client_request_id(f"root-failure-{failure_point}") is None


@pytest.mark.asyncio
async def test_failed_admission_compensation_failure_keeps_a_fail_closed_owner(
    monkeypatch,
) -> None:
    from modus.desktop import server
    from modus.desktop.db import get_run_by_client_request_id

    websocket = RecordingWebSocket()
    session = _fresh_session(monkeypatch, server)
    provider_starts = 0

    async def forbidden_submission(*args, **kwargs):
        nonlocal provider_starts
        provider_starts += 1
        raise AssertionError("indeterminate admission must not start provider code")

    monkeypatch.setattr(server, "_run_preallocated_submission", forbidden_submission)
    monkeypatch.setattr(server, "_canonical_run_admission", lambda *_args: False)

    def fail_compensation(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected admission compensation failure")

    monkeypatch.setattr(server, "fail_run_admission", fail_compensation)
    request = _explicit_message(session, request_id="compensation-failed")

    try:
        assert await server._handle_explicit_run_message(
            websocket, session, request,
        ) is True

        barrier = session.active_run_task
        assert barrier is not None
        await barrier
        assert barrier.done()
        assert session.active_run_task is barrier
        assert provider_starts == 0
        assert not any(packet["type"] == "run_accepted" for packet in websocket.sent)
        assert websocket.sent[-1]["type"] == "error"
        assert websocket.sent[-1]["code"] == "run_admission_failed"

        run = get_run_by_client_request_id("compensation-failed")
        assert run is not None and run["state"] == "running"

        second = _explicit_message(session, request_id="must-stay-blocked")
        assert await server._handle_explicit_run_message(
            websocket, session, second,
        ) is True
        assert websocket.sent[-1]["code"] == "session_busy"
        assert get_run_by_client_request_id("must-stay-blocked") is None
        assert provider_starts == 0
    finally:
        from modus.desktop import session_state

        rechecker = session.settlement_recheck_task
        if rechecker is not None and not rechecker.done():
            rechecker.cancel()
        session_state._active_persisted_runs.pop(session.db_id, None)
        session.active_run_task = None
        session.active_run_session_id = None
        session.active_run_id = None
        session.active_controller = None


@pytest.mark.asyncio
async def test_compatibility_runner_does_not_start_provider_without_durable_root(
    monkeypatch,
) -> None:
    from modus.desktop import server
    from modus.desktop.db import create_session, get_run, get_run_task

    class Engine:
        config = None

        def __init__(self) -> None:
            self.starts = 0

        async def ask(self, *_args, **_kwargs):
            self.starts += 1
            if False:  # pragma: no cover - async generator shape
                yield {}

    websocket = RecordingWebSocket()
    persisted = create_session("compatibility root failure")
    engine = Engine()
    session = server.DaoSession(
        id="runtime-compat-root-failure", db_id=persisted["id"], engine=engine,
    )
    monkeypatch.setattr(server, "create_run_admission", lambda *_args, **_kwargs: {})

    with pytest.raises(RuntimeError, match="could not persist its root task"):
        await server._stream_to_ws(websocket, session, "must not reach provider")

    assert engine.starts == 0
    run_id = session.active_run_id
    assert get_run(run_id) is None
    assert get_run_task(f"task_{run_id}_root") is None
    assert not any(packet["type"] == "agent_event" for packet in websocket.sent)


@pytest.mark.asyncio
async def test_run_accepted_transport_failure_fails_admission_before_provider_and_replays_error(
    monkeypatch,
) -> None:
    from modus.desktop import server
    from modus.desktop.db import get_run_by_client_request_id, get_run_task

    websocket = RejectAdmissionAckWebSocket()
    session = _fresh_session(monkeypatch, server)
    provider_starts = 0

    async def forbidden_submission(*args, **kwargs):
        nonlocal provider_starts
        provider_starts += 1
        raise AssertionError("provider must not start before run_accepted succeeds")

    async def skip_engine_rebuild(_session):
        return None

    monkeypatch.setattr(server, "_run_preallocated_submission", forbidden_submission)
    monkeypatch.setattr(server, "_rebuild_session_engine", skip_engine_rebuild)
    request = _explicit_message(session, request_id="ack-transport-failure")

    with pytest.raises(
        ConnectionError, match="injected run admission transport failure",
    ):
        await server._handle_explicit_run_message(websocket, session, request)

    task = session.active_run_task
    if task is not None:
        await task
    assert websocket.ack_attempts == 1
    assert provider_starts == 0
    assert session.active_run_task is None
    assert session.active_controller is None
    assert session.active_run_id is None
    assert not any(packet["type"] == "run_accepted" for packet in websocket.sent)

    run = get_run_by_client_request_id("ack-transport-failure")
    assert run is not None
    assert run["state"] == "failed"
    assert run["stop_reason"] == "admission_transport_failed"
    root = get_run_task(f"task_{run['run_id']}_root")
    assert root is not None and root["status"] == "failed"

    reconnect = server.manager.create(engine=object())
    replay = dict(request, runtime_session_id=reconnect.id)
    replay_websocket = RecordingWebSocket()
    assert await server._handle_explicit_run_message(
        replay_websocket, reconnect, replay,
    ) is True

    assert reconnect.db_id == run["session_id"]
    assert provider_starts == 0
    assert [packet["type"] for packet in replay_websocket.sent] == [
        "session_persisted", "error",
    ]
    assert replay_websocket.sent[-1]["code"] == "run_admission_failed"
    assert replay_websocket.sent[-1]["run_id"] == run["run_id"]
    assert not any(
        packet["type"] == "run_accepted" for packet in replay_websocket.sent
    )


@pytest.mark.asyncio
async def test_session_owner_rejection_atomically_fails_admission_and_replays_error(
    monkeypatch,
) -> None:
    from modus.desktop import server
    from modus.desktop.db import get_run_by_client_request_id, get_run_task

    websocket = RecordingWebSocket()
    session = _fresh_session(monkeypatch, server)
    provider_starts = 0

    async def forbidden_submission(*args, **kwargs):
        nonlocal provider_starts
        provider_starts += 1
        raise AssertionError("rejected session ownership must not start provider code")

    def reject_session_run(_session, coroutine, **kwargs):
        coroutine.close()
        return False

    monkeypatch.setattr(server, "_run_preallocated_submission", forbidden_submission)
    monkeypatch.setattr(server, "start_session_run", reject_session_run)
    request = _explicit_message(session, request_id="session-owner-conflict")

    assert await server._handle_explicit_run_message(
        websocket, session, request,
    ) is True

    assert provider_starts == 0
    assert session.active_run_task is None
    assert session.active_controller is None
    assert session.active_run_id is None
    assert not any(packet["type"] == "run_accepted" for packet in websocket.sent)
    assert websocket.sent[-1]["type"] == "error"
    assert websocket.sent[-1]["code"] == "session_busy"

    run = get_run_by_client_request_id("session-owner-conflict")
    assert run is not None
    assert run["state"] == "failed"
    assert run["stop_reason"] == "admission_conflict"
    root = get_run_task(f"task_{run['run_id']}_root")
    assert root is not None and root["status"] == "failed"

    replay_websocket = RecordingWebSocket()
    assert await server._handle_explicit_run_message(
        replay_websocket, session, request,
    ) is True
    assert provider_starts == 0
    assert replay_websocket.sent[-1]["type"] == "error"
    assert replay_websocket.sent[-1]["code"] == "run_admission_failed"
    assert replay_websocket.sent[-1]["run_id"] == run["run_id"]
    assert not any(
        packet["type"] == "run_accepted" for packet in replay_websocket.sent
    )


@pytest.mark.asyncio
async def test_duplicate_request_reuses_active_and_terminal_run_without_second_start(
    monkeypatch,
) -> None:
    from modus.desktop import server
    from modus.desktop.db import add_message, get_messages, list_runs_for_session
    from modus.desktop.events import Actor, ChannelId, EventType
    from modus.runtime.state import RunState

    websocket = RecordingWebSocket()
    session = _fresh_session(monkeypatch, server)
    entered = asyncio.Event()
    release = asyncio.Event()
    starts = 0

    async def fake_submission(ws, runner_session, content, skill_id, *, mode, emitter, controller):
        nonlocal starts
        starts += 1
        assert controller.state is RunState.RUNNING
        add_message(runner_session.db_id, "user", content)
        entered.set()
        await release.wait()
        await emitter.emit(
            EventType.RUN_COMPLETED, ChannelId.USER_HOST,
            Actor.host("primary", "主持人"),
            {"stop_reason": "completed", "budget": controller.budget.snapshot()},
        )
        controller.transition(RunState.COMPLETED)
        if runner_session.active_controller is controller:
            runner_session.active_controller = None
        return emitter

    monkeypatch.setattr(server, "_run_preallocated_submission", fake_submission)
    message = _explicit_message(session, request_id="retry-stable")

    await server._handle_explicit_run_message(websocket, session, message)
    task = session.active_run_task
    assert task is not None
    await entered.wait()

    await server._handle_explicit_run_message(websocket, session, message)
    active_acks = [packet for packet in websocket.sent if packet["type"] == "run_accepted"]
    assert [packet["duplicate"] for packet in active_acks] == [False, True]
    assert active_acks[0]["run_id"] == active_acks[1]["run_id"]
    assert active_acks[1]["state"] == "running"
    assert starts == 1

    conflict = dict(message, content="different task")
    await server._handle_explicit_run_message(websocket, session, conflict)
    assert websocket.sent[-1]["type"] == "error"
    assert websocket.sent[-1]["code"] == "request_id_conflict"

    release.set()
    await task
    await server._handle_explicit_run_message(websocket, session, message)

    all_acks = [packet for packet in websocket.sent if packet["type"] == "run_accepted"]
    assert len(all_acks) == 3
    assert all(packet["run_id"] == all_acks[0]["run_id"] for packet in all_acks)
    assert all_acks[-1]["duplicate"] is True
    assert all_acks[-1]["state"] == "completed"
    assert starts == 1
    assert len(list_runs_for_session(session.db_id)) == 1
    assert [message["content"] for message in get_messages(session.db_id)] == ["build it"]


@pytest.mark.asyncio
async def test_duplicate_request_reports_durable_failed_state_after_provider_eof(
    monkeypatch,
) -> None:
    from modus.desktop import server
    from modus.desktop.db import get_run

    class EofEngine:
        config = None

        def __init__(self) -> None:
            self.starts = 0

        async def ask(
            self, message, history=None, *, approval_callback=None,
            cancel_event=None, budget=None, session_id=None, run_id=None,
        ):
            self.starts += 1
            if False:  # pragma: no cover - keeps this an async generator
                yield {}

    websocket = RecordingWebSocket()
    session = _fresh_session(monkeypatch, server)
    engine = EofEngine()
    session.engine = engine
    message = _explicit_message(session, request_id="provider-eof-retry")

    assert await server._handle_explicit_run_message(websocket, session, message) is True
    task = session.active_run_task
    assert task is not None
    await task

    first_ack = next(packet for packet in websocket.sent if packet["type"] == "run_accepted")
    run = get_run(first_ack["run_id"])
    assert run is not None and run["state"] == "failed"
    assert run["stop_reason"] == "engine_error"

    assert await server._handle_explicit_run_message(websocket, session, message) is True
    acks = [packet for packet in websocket.sent if packet["type"] == "run_accepted"]

    assert engine.starts == 1
    assert len(acks) == 2
    assert acks[1]["run_id"] == acks[0]["run_id"]
    assert acks[1]["duplicate"] is True
    assert acks[1]["state"] == acks[1]["status"] == "failed"
    assert acks[1]["stop_reason"] == "engine_error"
    assert acks[1]["owned"] is False
    assert acks[1]["run_owned_by_connection"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        ({"runtime_session_id": "another-runtime"}, "runtime_session_mismatch"),
        ({"db_id": "another-session", "session_id": "another-session"}, "session_mismatch"),
        ({"db_id": "one-session", "session_id": "other-session"}, "session_mismatch"),
    ],
)
async def test_mismatched_submission_identity_has_no_persistence_or_runner_side_effect(
    monkeypatch, overrides: dict, error_code: str,
) -> None:
    from modus.desktop import server
    from modus.desktop.db import get_run_by_client_request_id, list_sessions

    websocket = RecordingWebSocket()
    session = _fresh_session(monkeypatch, server)
    starts = 0

    async def forbidden_runner(*args, **kwargs):
        nonlocal starts
        starts += 1
        raise AssertionError("identity mismatch must not reach a runner")

    monkeypatch.setattr(server, "_run_preallocated_submission", forbidden_runner)
    message = _explicit_message(session, request_id="wrong-identity")
    message.update(overrides)

    handled = await server._handle_explicit_run_message(websocket, session, message)

    assert handled is True
    assert websocket.sent[-1]["type"] == "error"
    assert websocket.sent[-1]["code"] == error_code
    assert session.db_id == ""
    assert session.active_run_task is None
    assert starts == 0
    assert list_sessions(include_archived=True) == []
    assert get_run_by_client_request_id("wrong-identity") is None


def test_old_database_migrates_request_columns_and_partial_unique_index(
    monkeypatch, tmp_path,
) -> None:
    from modus.desktop import db

    legacy_dir = tmp_path / "legacy-modus"
    legacy_dir.mkdir()
    legacy_path = legacy_dir / "desktop.db"
    with sqlite3.connect(legacy_path) as conn:
        conn.execute(
            """CREATE TABLE runs (
                   run_id TEXT PRIMARY KEY,
                   session_id TEXT NOT NULL,
                   mode TEXT NOT NULL,
                   state TEXT NOT NULL DEFAULT 'running',
                   stop_reason TEXT,
                   config_snapshot TEXT NOT NULL DEFAULT '{}',
                   budget TEXT NOT NULL DEFAULT '{}',
                   error TEXT,
                   started_at REAL NOT NULL,
                   updated_at REAL NOT NULL,
                   ended_at REAL
               )""",
        )
        conn.execute(
            """INSERT INTO runs
               (run_id, session_id, mode, state, started_at, updated_at)
               VALUES ('legacy-run', 'legacy-session', 'default', 'completed', 1, 1)""",
        )

    monkeypatch.setattr(db, "DB_DIR", legacy_dir)
    monkeypatch.setattr(db, "DB_PATH", legacy_path)
    db.init_db()

    with sqlite3.connect(legacy_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info('runs')")}
        indexes = {
            row[1]: {"unique": bool(row[2]), "partial": bool(row[4])}
            for row in conn.execute("PRAGMA index_list('runs')")
        }

    assert {"client_request_id", "client_request_fingerprint"} <= columns
    assert indexes["idx_runs_client_request"] == {"unique": True, "partial": True}
    assert db.get_run("legacy-run")["client_request_id"] is None

    session = db.create_session("migration target")
    first = db.create_run(
        "run-one", session["id"], "default",
        client_request_id="one-intent", client_request_fingerprint="fingerprint",
    )
    duplicate = db.create_run(
        "run-two", session["id"], "default",
        client_request_id="one-intent", client_request_fingerprint="fingerprint",
    )
    assert first["run_id"] == "run-one"
    assert duplicate == {}
    assert db.get_run_by_client_request_id("one-intent")["run_id"] == "run-one"
