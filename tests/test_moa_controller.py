import pytest

from modus.desktop.events import RunEventEmitter
from modus.runtime.controller import RunController
from modus.runtime.state import RunState
from modus.types import Message


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


def _models(_session=None, _mode=None) -> dict:
    model = {"id": "a", "model_id": "a", "name": "Architect", "provider": "test", "model": "a", "api_key": "key"}
    return {
        "moa_roles": {"host": dict(model), "reference_1": dict(model)},
    }


@pytest.mark.asyncio
async def test_moa_uses_supplied_controller_and_leaves_terminal_ownership_to_caller(monkeypatch):
    from modus.desktop import server

    monkeypatch.setattr(server, "_load_models_for_session", _models)

    def fake_reference(*args, **kwargs):
        return "reference advice"

    async def fake_host(*args, **kwargs):
        return "host synthesis"

    monkeypatch.setattr("modus.agent.moa.call_reference", fake_reference)
    async def fake_aggregator(*args, **kwargs):
        return "aggregator guidance"
    monkeypatch.setattr("modus.agent.moa.call_aggregator", fake_aggregator)
    monkeypatch.setattr("modus.agent.moa.call_host", fake_host)
    socket = FakeWebSocket()
    emitter = RunEventEmitter(run_id="run-moa", mode="moa", send_json=socket.send_json)
    session = server.DaoSession(id="s", db_id="db")
    controller = RunController(run_id="run-moa", mode="moa")

    await server._run_moa_stream(
        socket, session, [Message(role="user", content="design it")],
        emitter=emitter, controller=controller,
    )

    assert controller.state is RunState.RUNNING
    assert session.active_controller is controller


@pytest.mark.asyncio
async def test_moa_owned_controller_is_active_until_aggregation_then_cleared(monkeypatch):
    from modus.desktop import server

    monkeypatch.setattr(server, "_load_models_for_session", _models)

    def fake_reference(*args, **kwargs):
        return "reference advice"

    async def fake_host(*args, **kwargs):
        return "host synthesis"

    monkeypatch.setattr("modus.agent.moa.call_reference", fake_reference)
    async def fake_aggregator2(*args, **kwargs):
        return "aggregator guidance"
    monkeypatch.setattr("modus.agent.moa.call_aggregator", fake_aggregator2)
    monkeypatch.setattr("modus.agent.moa.call_host", fake_host)
    socket = FakeWebSocket()
    session = server.DaoSession(id="s", db_id="db")
    observed: list[RunState] = []
    original_emit = server.RunEventEmitter.emit

    async def capture_aggregation(self, event_type, *args, **kwargs):
        if event_type.value == "host_aggregation":
            assert session.active_controller is not None
            observed.append(session.active_controller.state)
        return await original_emit(self, event_type, *args, **kwargs)

    monkeypatch.setattr(server.RunEventEmitter, "emit", capture_aggregation)
    await server._run_moa_stream(socket, session, [Message(role="user", content="design it")])

    assert observed
    assert all(state is RunState.RUNNING for state in observed)
    assert session.active_controller is None


@pytest.mark.asyncio
async def test_moa_passes_role_owner_keys_into_reference_and_aggregator_usage(monkeypatch):
    from modus.desktop import server
    from modus.runtime.budget import RunBudget

    monkeypatch.setattr(server, "_load_models_for_session", _models)
    owners: dict[str, list[str]] = {"reference": [], "aggregator": []}

    async def fake_reference(*args, **kwargs):
        owners["reference"].append(kwargs.get("owner"))
        return "advice"

    async def fake_aggregator(*args, **kwargs):
        owners["aggregator"].append(kwargs.get("owner"))
        return "guidance"

    async def fake_host(*args, **kwargs):
        return "synthesis"

    monkeypatch.setattr("modus.agent.moa.call_reference", fake_reference)
    monkeypatch.setattr("modus.agent.moa.call_aggregator", fake_aggregator)
    monkeypatch.setattr("modus.agent.moa.call_host", fake_host)

    socket = FakeWebSocket()
    emitter = RunEventEmitter(run_id="run-moa", mode="moa", send_json=socket.send_json)
    session = server.DaoSession(id="s", db_id="db")
    controller = RunController(run_id="run-moa", mode="moa", budget=RunBudget())
    await server._run_moa_stream(
        socket, session, [Message(role="user", content="design it")],
        emitter=emitter, controller=controller,
    )

    assert owners["reference"], "reference calls must carry an owner key"
    assert all(owner == "reference_1" for owner in owners["reference"])
    assert owners["aggregator"] == ["aggregator"]
