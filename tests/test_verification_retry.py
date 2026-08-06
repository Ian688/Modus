from fastapi.testclient import TestClient
import asyncio

import pytest


def _receive_until(socket, target: str, limit: int = 30):
    packets = []
    for _ in range(limit):
        packet = socket.receive_json()
        packets.append(packet)
        if packet.get("type") == target:
            return packets
    raise AssertionError(f"did not receive {target}: {packets!r}")


def test_verification_retry_is_scoped_to_session_and_starts_a_new_run(monkeypatch):
    from modus.desktop import server
    from modus.desktop.db import create_run, create_session, update_run
    from modus.desktop.events import Actor, ChannelId, EventType
    from modus.runtime.state import RunState

    async def fake_registry(**_kwargs):
        return object()

    class FakeEngine:
        def __init__(self, **_kwargs):
            pass

        async def ask(self, message, history=None, **_kwargs):
            yield {"type": "done", "messages": [], "total_tokens": 0, "total_turns": 1}

    started: list[tuple[str, str]] = []

    async def fake_stream(_websocket, session, message, **kwargs):
        started.append((session.db_id, message))
        emitter = kwargs["emitter"]
        controller = kwargs["controller"]
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

    monkeypatch.setattr(server, "build_tool_registry", fake_registry)
    monkeypatch.setattr(server, "create_llm_client", lambda _cfg: object())
    monkeypatch.setattr(server, "QueryEngine", FakeEngine)
    monkeypatch.setattr(server, "_stream_to_ws", fake_stream)

    session = create_session(title="retry target")
    run_id = "run-verification-failed"
    create_run(run_id, session["id"], "default")
    update_run(
        run_id, state="failed", stop_reason="verification_retry_limit",
        budget={"verification": {"attempts": 3, "max_attempts": 3}},
    )

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "resume_session", "db_id": session["id"]})
            _receive_until(socket, "session_restored")
            socket.send_json({"type": "retry_verification", "run_id": run_id})
            packets = _receive_until(socket, "verification_retry_started")

    assert started and started[0][0] == session["id"]
    assert "必须调用 run_tests" in started[0][1]
    assert packets[-1]["prior_run_id"] == run_id


def test_verification_retry_rejects_a_run_from_another_session(monkeypatch):
    from modus.desktop import server
    from modus.desktop.db import create_run, create_session, update_run

    async def fake_registry(**_kwargs):
        return object()

    class FakeEngine:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(server, "build_tool_registry", fake_registry)
    monkeypatch.setattr(server, "create_llm_client", lambda _cfg: object())
    monkeypatch.setattr(server, "QueryEngine", FakeEngine)

    owner = create_session(title="owner")
    other = create_session(title="other")
    run_id = "run-owned-by-other"
    create_run(run_id, other["id"], "default")
    update_run(run_id, state="failed", stop_reason="verification_required")

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "resume_session", "db_id": owner["id"]})
            _receive_until(socket, "session_restored")
            socket.send_json({"type": "retry_verification", "run_id": run_id})
            packets = _receive_until(socket, "error")

    assert packets[-1]["code"] == "verification_retry_not_found"


class _RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, packet: dict) -> None:
        self.sent.append(packet)


@pytest.mark.asyncio
async def test_verification_retry_persists_canonical_root_and_acks_before_provider(
    monkeypatch,
) -> None:
    from modus.desktop import server
    from modus.desktop.db import (
        create_run, create_session, get_run, get_run_task, update_run,
    )
    from modus.desktop.events import Actor, ChannelId, EventType
    from modus.runtime.state import RunState

    persisted = create_session(title="verification retry admission")
    prior_run_id = "prior-verification-failed"
    create_run(prior_run_id, persisted["id"], "default")
    update_run(
        prior_run_id, state="failed", stop_reason="verification_required",
    )
    session = server.DaoSession(
        id="runtime-verification-retry", db_id=persisted["id"], engine=object(),
    )
    websocket = _RecordingWebSocket()
    provider_starts = 0

    async def fake_stream(ws, runner_session, retry_message, **kwargs):
        nonlocal provider_starts
        provider_starts += 1
        emitter = kwargs["emitter"]
        controller = kwargs["controller"]
        assert ws is websocket
        assert runner_session is session
        assert "必须调用 run_tests" in retry_message
        assert kwargs["persisted_run"] is True
        assert kwargs["verification_required"] is True
        assert kwargs["manage_controller"] is True
        assert controller.state is RunState.RUNNING
        run = get_run(emitter.run_id)
        root = get_run_task(f"task_{emitter.run_id}_root")
        assert run is not None and run["state"] == "running"
        assert run["config_snapshot"]["verification"]["required"] is True
        assert root is not None and root["status"] == "running"
        assert websocket.sent[-1]["type"] == "verification_retry_started"
        await emitter.emit(
            EventType.RUN_COMPLETED,
            ChannelId.USER_HOST,
            Actor.host("primary", "主持人"),
            {"stop_reason": "completed", "budget": {}},
        )
        controller.transition(RunState.COMPLETED)
        if runner_session.active_controller is controller:
            runner_session.active_controller = None
        return emitter

    monkeypatch.setattr(server, "_stream_to_ws", fake_stream)
    await server._handle_verification_retry(
        websocket, session,
        {"type": "retry_verification", "run_id": prior_run_id},
    )
    task = session.active_run_task
    assert task is not None
    await task

    ack = next(
        packet for packet in websocket.sent
        if packet["type"] == "verification_retry_started"
    )
    assert ack["prior_run_id"] == prior_run_id
    assert ack["run_id"] != prior_run_id
    assert provider_starts == 1
    assert session.active_run_task is None


@pytest.mark.asyncio
async def test_verification_retry_root_failure_never_starts_provider(
    monkeypatch,
) -> None:
    from modus.desktop import server
    from modus.desktop.db import (
        create_run, create_session, get_run, update_run,
    )

    persisted = create_session(title="verification retry root failure")
    prior_run_id = "prior-needs-verification"
    create_run(prior_run_id, persisted["id"], "default")
    update_run(
        prior_run_id, state="failed", stop_reason="verification_retry_limit",
    )
    session = server.DaoSession(
        id="runtime-verification-root-failure",
        db_id=persisted["id"], engine=object(),
    )
    websocket = _RecordingWebSocket()
    provider_starts = 0

    async def forbidden_stream(*args, **kwargs):
        nonlocal provider_starts
        provider_starts += 1
        raise AssertionError("verification provider started without canonical root")

    monkeypatch.setattr(server, "_stream_to_ws", forbidden_stream)
    monkeypatch.setattr(server, "create_run_admission", lambda *_args, **_kwargs: {})
    await server._handle_verification_retry(
        websocket, session,
        {"type": "retry_verification", "run_id": prior_run_id},
    )

    assert provider_starts == 0
    assert session.active_run_task is None
    assert websocket.sent[-1]["type"] == "error"
    assert websocket.sent[-1]["code"] == "verification_retry_admission_failed"
    retry_run = get_run(websocket.sent[-1]["run_id"])
    assert retry_run is None
