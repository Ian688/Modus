import pytest


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, packet: dict) -> None:
        self.sent.append(packet)


async def _ignore_catalog_broadcast(*_args, **_kwargs) -> None:
    return None


@pytest.mark.asyncio
async def test_moa_provider_runtime_error_is_persisted_as_failure_not_disconnect(
    monkeypatch,
):
    from modus.config import ModusConfig
    from modus.desktop import db, server

    class Engine:
        config = ModusConfig()

    monkeypatch.setattr(server, "_broadcast_sessions_list", _ignore_catalog_broadcast)
    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "moa_roles": {
            "host": {
                "id": "host", "model_id": "host", "name": "Host",
                "provider": "test", "model": "host",
            },
            "reference_1": {
                "id": "reference", "model_id": "reference", "name": "Reference",
                "provider": "test", "model": "reference",
            },
        },
    })

    async def reference_succeeds(*_args, **_kwargs):
        return "reference evidence"

    async def aggregator_fails(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("modus.agent.moa.call_reference", reference_succeeds)
    monkeypatch.setattr("modus.agent.moa.call_aggregator", aggregator_fails)

    persisted = db.create_session("MOA provider failure")
    session = server.DaoSession(
        id="runtime-moa-provider-failure", db_id=persisted["id"], engine=Engine(),
    )
    websocket = FakeWebSocket()

    await server._run_moa_session(websocket, session, "task")

    run = db.get_run(session.active_run_id)
    terminals = [
        event for event in db.get_run_events(session.active_run_id)
        if event["type"] in {"run_completed", "run_error"}
    ]
    assert run is not None and run["state"] == "failed"
    assert run["stop_reason"] == "failed"
    assert run["budget"]["stop_reason"] == "failed"
    assert len(terminals) == 1
    assert terminals[0]["type"] == "run_error"
    assert terminals[0]["status"] == "failed"
    assert terminals[0]["payload"]["code"] == "moa_failed"
    assert terminals[0]["payload"]["stop_reason"] == "failed"
    assert not any(
        event["payload"].get("code") == "transport_disconnected"
        for event in terminals
    )


@pytest.mark.asyncio
async def test_peri_provider_runtime_error_is_persisted_as_failure_not_disconnect(
    monkeypatch,
):
    from modus.config import ModusConfig
    from modus.desktop import db, server

    class Engine:
        config = ModusConfig()

    monkeypatch.setattr(server, "_broadcast_sessions_list", _ignore_catalog_broadcast)
    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "peri_roles": {
            "host": {
                "id": "host", "model_id": "host", "name": "Host",
                "provider": "test", "model": "host",
            },
            "worker_1": {
                "id": "worker", "model_id": "worker", "name": "Worker",
                "provider": "test", "model": "worker",
            },
        },
    })

    async def decomposition_fails(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("modus.desktop.peri.decompose_task", decomposition_fails)

    persisted = db.create_session("Peri provider failure")
    session = server.DaoSession(
        id="runtime-peri-provider-failure", db_id=persisted["id"], engine=Engine(),
    )
    websocket = FakeWebSocket()

    await server._run_peri_session(websocket, session, "task")

    run = db.get_run(session.active_run_id)
    terminals = [
        event for event in db.get_run_events(session.active_run_id)
        if event["type"] in {"run_completed", "run_error"}
    ]
    assert run is not None and run["state"] == "failed"
    assert run["stop_reason"] == "failed"
    assert run["budget"]["stop_reason"] == "failed"
    assert len(terminals) == 1
    assert terminals[0]["type"] == "run_error"
    assert terminals[0]["status"] == "failed"
    assert terminals[0]["payload"]["code"] == "peri_failed"
    assert terminals[0]["payload"]["stop_reason"] == "failed"
    assert not any(
        event["payload"].get("code") == "transport_disconnected"
        for event in terminals
    )
