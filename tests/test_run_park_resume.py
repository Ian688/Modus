"""Run park & resume: disconnect keeps the run executing, reconnect resumes it.

Covers the park_run / resume_parked session lifecycle, the emitter rebind,
and the PAUSED controller state.  The server-level reconnect path is covered
by the session-resume contract; these tests exercise the parking primitives
and the resume_session handler's parked-run branch.
"""

from __future__ import annotations

import asyncio

import pytest

from modus.config import ModusConfig
from modus.desktop.events import RunEventEmitter
from modus.runtime.controller import RunController
from modus.runtime.state import RunState


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


def _make_session(config: ModusConfig | None = None):
    from modus.desktop.session_state import DaoSession

    session = DaoSession(id="sess", db_id="db")
    session.engine = type("Engine", (), {"config": config or ModusConfig()})()
    return session


def _make_emitter(send_json) -> RunEventEmitter:
    return RunEventEmitter(run_id="run_park", mode="default", send_json=send_json)


def test_park_run_sets_paused_and_detaches_transport():
    session = _make_session()
    original_socket = FakeSocket()
    emitter = _make_emitter(original_socket.send_json)
    controller = RunController(run_id="run_park", mode="default")
    controller.transition(RunState.RUNNING)

    session.park_run(emitter, controller)

    assert session.parked is True
    assert session.parked_emitter is emitter
    assert controller.state is RunState.PAUSED
    # A subsequent emit must not raise on the parked (no-op) transport.
    asyncio.run(_parked_emit(emitter))


async def _parked_emit(emitter: RunEventEmitter) -> None:
    from modus.desktop.events import Actor, ChannelId, EventType

    await emitter.emit(
        EventType.SUBAGENT_RESPONSE, ChannelId.HOST_MODELS, Actor.system(), {"markdown": "x"},
    )


def test_resume_parked_rebinds_emitter_and_reactivates():
    session = _make_session()
    dead_socket = FakeSocket()
    live_socket = FakeSocket()
    emitter = _make_emitter(dead_socket.send_json)
    controller = RunController(run_id="run_park", mode="default")
    controller.transition(RunState.RUNNING)
    session.park_run(emitter, controller)

    resumed = session.resume_parked(live_socket.send_json)

    assert resumed is True
    assert session.parked is False
    assert controller.state is RunState.RUNNING
    asyncio.run(_emit_to(live_socket, emitter))
    assert any(item["type"] == "agent_event" for item in live_socket.sent)
    assert dead_socket.sent == []


async def _emit_to(socket: FakeSocket, emitter: RunEventEmitter) -> None:
    from modus.desktop.events import Actor, ChannelId, EventType

    await emitter.emit(
        EventType.SUBAGENT_RESPONSE, ChannelId.HOST_MODELS, Actor.system(), {"markdown": "y"},
    )


def test_resume_parked_when_not_parked_returns_false():
    session = _make_session()
    socket = FakeSocket()
    assert session.resume_parked(socket.send_json) is False


def test_handle_disconnect_cancels_when_park_disabled():
    session = _make_session()  # park_on_disconnect defaults False
    controller = RunController(run_id="run_park", mode="default")
    controller.transition(RunState.RUNNING)
    session.active_controller = controller
    emitter = _make_emitter(FakeSocket().send_json)

    parked = session.handle_disconnect(emitter, controller)

    assert parked is False
    assert session.parked is False
    assert controller.state is RunState.CANCELLING


def test_handle_disconnect_parks_when_enabled():
    config = ModusConfig()
    config.features.park_on_disconnect = True
    session = _make_session(config)
    controller = RunController(run_id="run_park", mode="default")
    controller.transition(RunState.RUNNING)
    emitter = _make_emitter(FakeSocket().send_json)

    parked = session.handle_disconnect(emitter, controller)

    assert parked is True
    assert session.parked is True
    assert controller.state is RunState.PAUSED


def test_emitter_rebind_swaps_send_target():
    first = FakeSocket()
    second = FakeSocket()
    emitter = _make_emitter(first.send_json)

    emitter.rebind(second.send_json)
    asyncio.run(_emit_to(second, emitter))

    assert first.sent == []
    assert len(second.sent) == 1
