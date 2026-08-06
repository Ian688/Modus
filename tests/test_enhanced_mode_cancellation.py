import asyncio

import pytest

from modus.desktop.events import RunEventEmitter
from modus.runtime.controller import RunController
from modus.runtime.state import RunState


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, packet):
        self.sent.append(packet)


@pytest.mark.asyncio
async def test_moa_cancel_reaps_stalled_reference(monkeypatch):
    from modus.desktop import server

    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "moa_roles": {
            "host": {"id": "host", "model_id": "host", "name": "Host", "provider": "test", "model": "host", "api_key": "key"},
            "reference_1": {"id": "ref", "model_id": "ref", "name": "Ref", "provider": "test", "model": "ref", "api_key": "key"},
        },
    })

    started = asyncio.Event()
    reaped = asyncio.Event()

    async def stalled_reference(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            reaped.set()

    monkeypatch.setattr("modus.agent.moa.call_reference", stalled_reference)
    socket = FakeWebSocket()
    session = server.DaoSession(id="s", db_id="db")
    controller = RunController(run_id="run-cancel-moa", mode="moa")
    emitter = RunEventEmitter(run_id=controller.run_id, mode="moa", send_json=socket.send_json)

    waiting = asyncio.create_task(server._run_moa_stream(
        socket, session, [], emitter=emitter, controller=controller,
    ))
    await started.wait()
    controller.cancel()
    assert await asyncio.wait_for(waiting, timeout=1) == ""
    assert reaped.is_set()
    assert controller.state is RunState.CANCELLING

@pytest.mark.asyncio
async def test_peri_cancel_reaps_stalled_worker(monkeypatch):
    from modus.desktop import server

    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "peri_roles": {
            "host": {"id": "host", "model_id": "host", "provider": "test", "model": "host", "api_key": "key"},
            "worker_1": {"id": "worker", "model_id": "worker", "provider": "test", "model": "worker", "api_key": "key"},
        },
    })

    async def decompose(*args, **kwargs):
        return [{"name": "Work", "description": "work", "context": "ctx"}]

    started = asyncio.Event()
    reaped = asyncio.Event()

    async def stalled_worker(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            reaped.set()

    monkeypatch.setattr("modus.desktop.peri.decompose_task", decompose)
    monkeypatch.setattr("modus.desktop.peri.execute_subtask", stalled_worker)
    socket = FakeWebSocket()
    session = server.DaoSession(id="s", db_id="db")
    controller = RunController(run_id="run-cancel-tele", mode="peri")
    emitter = RunEventEmitter(run_id=controller.run_id, mode="peri", send_json=socket.send_json)

    waiting = asyncio.create_task(server._run_peri_stream(
        socket, session, "task", emitter=emitter, controller=controller,
    ))
    await started.wait()
    controller.cancel()
    await asyncio.wait_for(waiting, timeout=1)
    assert reaped.is_set()
    assert controller.state is RunState.CANCELLING
