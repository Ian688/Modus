import pytest

from modus.types import Message


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_moa_emits_host_model_events_in_one_shared_run(monkeypatch):
    from modus.desktop import server
    from modus.desktop.events import RunEventEmitter

    websocket = FakeWebSocket()
    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "moa_roles": {
            "host": {"id": "host", "model_id": "host", "name": "Host", "provider": "test", "model": "h", "api_key": "key"},
            "reference_1": {"id": "a", "model_id": "a", "name": "Architect", "provider": "test", "model": "a", "api_key": "key"},
            "reference_2": {"id": "b", "model_id": "b", "name": "Reviewer", "provider": "test", "model": "b", "api_key": "key"},
        },
    })

    async def fake_reference(ref, messages, prompt, temperature=0.7, timeout=25.0, *, stream_callback=None, owner=None):
        if stream_callback:
            await stream_callback(f"Advice from {ref['name']}")
        return f"Advice from {ref['name']}"

    monkeypatch.setattr("modus.agent.moa.call_reference", fake_reference)

    async def fake_aggregator(host_config, messages, system_prompt, reference_outputs, temperature=0.4, timeout=60.0, *, stream_callback=None, owner=None):
        if stream_callback:
            await stream_callback("Aggregator guidance")
        return "Aggregator guidance"
    monkeypatch.setattr("modus.agent.moa.call_aggregator", fake_aggregator)

    async def fake_host(host_config, messages, system_prompt, guidance, temperature=0.7, timeout=60.0, *, stream_callback=None, owner=None):
        if stream_callback:
            await stream_callback("Host synthesis result")
        return "Host synthesis result"
    monkeypatch.setattr("modus.agent.moa.call_host", fake_host)

    emitter = RunEventEmitter(run_id="run_moa", mode="moa", send_json=websocket.send_json)
    session = server.DaoSession(id="s", db_id="db")
    guidance = await server._run_moa_stream(
        websocket, session, [Message(role="user", content="Build a game")], emitter=emitter
    )

    events = [item["event"] for item in websocket.sent if item["type"] == "agent_event"]
    assert {event["run_id"] for event in events} == {"run_moa"}
    channels = {event["channel_id"] for event in events}
    assert "host_models" in channels
    # MOA runner no longer emits host_response (that's the default phase)
    agg_events = [e for e in events if e["type"] == "host_aggregation"]
    assert len(agg_events) >= 1

    # Check reference events exist
    ref_events = [e for e in events if e["type"] == "reference_response"]
    assert len(ref_events) >= 2
    identity_events = [
        event for event in events
        if event["type"] in {"host_dispatch", "reference_started", "reference_response"}
    ]
    assert identity_events
    assert all(event["payload"].get("target_id") for event in identity_events)


@pytest.mark.asyncio
async def test_moa_with_no_host_model_reports_error(monkeypatch):
    from modus.desktop import server
    from modus.desktop.events import RunEventEmitter

    websocket = FakeWebSocket()
    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {"moa_roles": {}})

    emitter = RunEventEmitter(run_id="run_empty", mode="moa", send_json=websocket.send_json)
    await server._run_moa_stream(websocket, server.DaoSession(id="s", db_id="db"), [], emitter=emitter)

    typed = [item["event"] for item in websocket.sent if item["type"] == "agent_event"]
    assert len(typed) == 1
    assert typed[0]["channel_id"] == "host_models"
    assert typed[0]["type"] == "run_error"
    assert "no_host_model" in typed[0]["payload"]["code"]
    # Aggregation is only a phase. It must not emit a transport terminal before
    # the accountable host has produced the final response.
    types = [m["type"] for m in websocket.sent]
    assert "moa_done" not in types


@pytest.mark.asyncio
async def test_full_moa_session_does_not_fall_through_to_default_after_configuration_failure(monkeypatch):
    from modus.desktop import server
    from modus.desktop import db
    from modus.config import ModusConfig

    class Repo:
        def runtime_mode_configuration(self, _mode, _snapshot=None):
            return {}

        def public_snapshot(self):
            return {"models": [], "selection": {"default_model_id": None, "moa_roles": {}}}

    class Engine:
        config = ModusConfig()

        async def ask(self, *args, **kwargs):
            raise AssertionError("default host must not run after MOA configuration failure")
            yield

    monkeypatch.setattr(server, "model_repository", Repo())
    websocket = FakeWebSocket()
    persisted = db.create_session("moa failure")
    session = server.DaoSession(id="s", db_id=persisted["id"], engine=Engine())

    await server._run_moa_session(websocket, session, "task")

    events = [item["event"] for item in websocket.sent if item["type"] == "agent_event"]
    assert [event["type"] for event in events] == ["run_started", "user_message", "run_error"]
    assert [item for item in websocket.sent if item["type"] == "done"][-1]["stop_reason"] == "failed"


@pytest.mark.asyncio
async def test_full_moa_transport_failure_persists_one_atomic_cancel_terminal(monkeypatch):
    from fastapi import WebSocketDisconnect

    from modus.config import ModusConfig
    from modus.desktop import db, server

    class Repo:
        def runtime_mode_configuration(self, _mode, _snapshot=None):
            return {
                "host": {
                    "id": "host", "model_id": "host", "name": "Host",
                    "provider": "test", "model": "host",
                },
            }

        def public_snapshot(self):
            return {"models": [], "selection": {"default_model_id": None}}

    class Engine:
        config = ModusConfig()

    class DisconnectingWebSocket:
        def __init__(self):
            self.sent = []

        async def send_json(self, packet):
            self.sent.append(packet)
            if packet["type"] == "agent_event" and packet["event"]["type"] == "host_aggregation":
                raise WebSocketDisconnect()

    monkeypatch.setattr(server, "model_repository", Repo())
    persisted = db.create_session("MOA disconnect")
    session = server.DaoSession(
        id="runtime-moa-disconnect", db_id=persisted["id"], engine=Engine(),
    )
    websocket = DisconnectingWebSocket()

    await server._run_moa_session(websocket, session, "task")

    run_id = session.active_run_id
    run = db.get_run(run_id)
    root = db.get_run_task(f"task_{run_id}_root")
    terminal = [
        event for event in db.get_run_events(run_id)
        if event["type"] in {"run_completed", "run_error"}
    ]
    assert run is not None and run["state"] == "cancelled"
    assert run["stop_reason"] == "cancelled"
    assert root is not None and root["status"] == "cancelled"
    assert len(terminal) == 1
    assert terminal[0]["type"] == "run_error"
    assert terminal[0]["status"] == "cancelled"
    assert terminal[0]["payload"]["code"] == "transport_disconnected"


def test_run_message_routes_moa_through_one_shared_session_runner():
    source = (__import__("pathlib").Path(__file__).parents[1] / "src/modus/desktop/server.py").read_text()
    handler = source[source.index('elif msg_type == "run_message":'):source.index('elif msg_type == "cancel":')]
    assert "await _handle_explicit_run_message(websocket, session, msg)" in handler
    admission = source[source.index("async def _run_preallocated_submission("):source.index("async def _send_duplicate_run_admission(")]
    assert "normalize_mode(session.mode)" not in handler
    assert "_run_moa_session" in admission
    assert "_run_peri_session" in admission
    assert "persisted_run=True" in admission
    assert "await _run_moa_stream" not in handler


@pytest.mark.asyncio
async def test_moa_send_failure_cancels_and_reaps_reference_siblings(monkeypatch):
    """A broken event sink must not leave another provider task detached."""
    import asyncio

    from modus.desktop.events import RunEventEmitter
    from modus.desktop.moa_runner import run_moa_stream
    from modus.desktop.session_state import DaoSession
    from modus.runtime.controller import RunController

    class FailingEmitter(RunEventEmitter):
        async def emit(self, event_type, channel_id, actor, payload, **kwargs):
            if (
                str(event_type) == "reference_response"
                and str(kwargs.get("status")) == "streaming"
                and payload.get("markdown") == "break transport"
            ):
                raise RuntimeError("socket closed")
            return await super().emit(event_type, channel_id, actor, payload, **kwargs)

    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    release_sibling = asyncio.Event()
    provider_calls: list[str] = []

    async def fake_reference(ref, *_args, stream_callback=None, **_kwargs):
        provider_calls.append(ref["id"])
        if ref["id"] == "slow":
            sibling_started.set()
            try:
                await release_sibling.wait()
                if stream_callback:
                    await stream_callback("late reference event")
                return "late reference artifact"
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise
        await sibling_started.wait()
        await stream_callback("break transport")
        return "unreachable"

    async def unexpected_aggregator(*_args, **_kwargs):
        raise AssertionError("aggregator must not run after event delivery fails")

    models = {
        "moa_roles": {
            "host": {"id": "host", "provider": "test", "model": "host"},
            "reference_1": {"id": "fail", "name": "Fail", "provider": "test", "model": "fail"},
            "reference_2": {"id": "slow", "name": "Slow", "provider": "test", "model": "slow"},
        },
    }
    sent: list[dict] = []

    async def send_json(packet: dict) -> None:
        sent.append(packet)

    monkeypatch.setattr("modus.agent.moa.call_reference", fake_reference)
    monkeypatch.setattr("modus.agent.moa.call_aggregator", unexpected_aggregator)
    emitter = FailingEmitter(run_id="run_moa_reap", mode="moa", send_json=send_json)
    session = DaoSession(id="runtime", db_id="")

    with pytest.raises(RuntimeError, match="socket closed"):
        await run_moa_stream(
            object(), session, [Message(role="user", content="task")],
            emitter=emitter, controller=RunController(run_id=emitter.run_id, mode="moa"),
            audit_event_for=lambda _session: None, load_models=lambda: models,
        )

    assert provider_calls == ["fail", "slow"]
    assert sibling_cancelled.is_set()
    before_release = [
        item["event"] for item in sent
        if item["type"] == "agent_event"
        and item["event"]["type"] in {"reference_response", "artifact"}
        and item["event"]["actor"]["id"] == "slow"
    ]
    release_sibling.set()
    await asyncio.sleep(0.01)
    after_release = [
        item["event"] for item in sent
        if item["type"] == "agent_event"
        and item["event"]["type"] in {"reference_response", "artifact"}
        and item["event"]["actor"]["id"] == "slow"
    ]
    assert after_release == before_release, [
        (event["type"], event["status"], event["actor"]["id"], event["payload"])
        for event in after_release[len(before_release):]
    ]
    assert not any(
        "late reference" in str(item) or "late reference artifact" in str(item)
        for item in sent
    )
