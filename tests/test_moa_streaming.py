import pytest

from modus.types import Message


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_call_reference_forwards_each_stream_delta_and_returns_full_text(monkeypatch):
    from modus.agent import moa

    class FakeClient:
        async def chat(self, messages, tools, system_prompt):
            yield {"type": "text_delta", "text": "事件"}
            yield {"type": "text_delta", "text": "流输出"}

    monkeypatch.setattr(moa, "create_llm_client", lambda cfg: FakeClient())
    received: list[str] = []

    def capture(chunk: str) -> None:
        received.append(chunk)

    result = await moa.call_reference(
        {"provider": "test", "model": "reference", "api_key": "key"},
        [Message(role="user", content="test")],
        "system",
        stream_callback=capture,
    )

    assert received == ["事件", "流输出"]
    assert result == "事件流输出"


@pytest.mark.asyncio
async def test_moa_reference_uses_one_stable_streaming_event_then_completes(monkeypatch):
    from modus.desktop import server
    from modus.desktop.events import RunEventEmitter

    websocket = FakeWebSocket()
    monkeypatch.setattr(server, "_load_models_for_session", lambda _session, _mode: {
        "moa_roles": {
            "host": {"id": "host", "model_id": "host", "name": "Host", "provider": "test", "model": "h", "api_key": "key"},
            "reference_1": {"id": "ref", "model_id": "ref", "name": "Reference", "provider": "test", "model": "ref", "api_key": "key"},
        },
    })

    async def fake_reference(ref, messages, prompt, temperature=0.7, timeout=25.0, *, stream_callback=None, owner=None):
        if stream_callback:
            await stream_callback("第一段")
            await stream_callback("第二段")
        return "第一段第二段"

    monkeypatch.setattr("modus.agent.moa.call_reference", fake_reference)

    async def fake_aggregator(host_config, messages, system_prompt, reference_outputs, temperature=0.4, timeout=60.0, *, stream_callback=None, owner=None):
        if stream_callback:
            await stream_callback("Aggregator guidance")
        return "Aggregator guidance"
    monkeypatch.setattr("modus.agent.moa.call_aggregator", fake_aggregator)

    # MOA runner no longer calls call_host — it returns guidance
    emitter = RunEventEmitter(run_id="run_stream", mode="moa", send_json=websocket.send_json)
    session = server.DaoSession(id="s", db_id="db")
    guidance = await server._run_moa_stream(
        websocket, session, [Message(role="user", content="Build a game")], emitter=emitter
    )
    assert guidance == "Aggregator guidance"

    events = [item["event"] for item in websocket.sent if item["type"] == "agent_event"]
    reference_events = [event for event in events if event["type"] == "reference_response"]
    assert len(reference_events) == 4  # empty card, two deltas, final completion
    assert {event["event_id"] for event in reference_events}.__len__() == 1
    assert [event["status"] for event in reference_events] == [
        "streaming", "streaming", "streaming", "completed",
    ]
    assert [event["payload"]["markdown"] for event in reference_events] == [
        "", "第一段", "第一段第二段", "第一段第二段",
    ]
    assert [event["sequence"] for event in reference_events] == [4, 4, 4, 4]
    # MOA runner no longer emits host_response — default runner does that
    assert guidance == "Aggregator guidance"


def test_reference_stream_callback_is_optional_for_backward_compatibility():
    from inspect import signature
    from modus.agent.moa import call_reference

    parameter = signature(call_reference).parameters["stream_callback"]
    assert parameter.default is None
    assert parameter.kind.name == "KEYWORD_ONLY"
